from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.platform.admin_contracts import SystemScenePayload
from src.platform.auth.admin import AdminAuthorizationError
from src.platform.auth.admin_identity import AdminSessionPrincipal
from src.platform.auth.sessions import SessionAuthenticationError, SessionPrincipal
from src.platform.contracts import AdminContext
from src.platform.db_models import AssetRecord, AuditEventRecord, MediaObjectRecord
from src.platform.media_storage import CloudMediaStorage, MediaValidationError
from src.platform.settings import DeploymentSettings
from src.platform.system_scenes import (
    SystemSceneCatalogService,
    SystemSceneConflictError,
    SystemSceneNotFoundError,
)
from src.platform.system_scenes_api import install_cloud_system_scenes_api
from tests.test_content_api import FakeSessions
from tests.test_content_repositories import RepositoryDatabase, _create_scope


class SceneDatabase(RepositoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        AuditEventRecord.__table__.create(self.engine)


class FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(self, object_key: str, content: bytes, content_type: str) -> None:
        self.objects[object_key] = (content, content_type)

    def signed_get_url(self, object_key: str, expires_seconds: int) -> str:
        if object_key not in self.objects:
            raise RuntimeError("missing object")
        return f"https://objects.example.test/access?media={object_key.rsplit('/', 1)[-1]}&ttl={expires_seconds}"

    def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)


def _payload(**updates) -> SystemScenePayload:
    values = {
        "name": "雨夜街巷",
        "description": "霓虹灯映照的雨夜街道",
        "category": "城市",
        "tags": ["夜景", "雨天"],
        "prompt": "cinematic rainy street",
        "negative_prompt": "low quality",
        "style": "赛博朋克",
        "aspect_ratio": "16:9",
        "visibility": "enabled",
        "sort_order": 10,
        "schema_version": 1,
        "cover_media_id": None,
    }
    values.update(updates)
    return SystemScenePayload.model_validate(values)


@pytest.fixture
def scene_scope():
    database = SceneDatabase()
    admin_scope = _create_scope(database)
    user_scope = _create_scope(database)
    admin = AdminContext(admin_id="9001", session_id="9101", username="admin")
    object_store = FakeObjectStore()
    storage = CloudMediaStorage(database, object_store, namespace_prefix="lumenx")
    service = SystemSceneCatalogService(database, storage)
    yield database, admin, user_scope, object_store, storage, service
    database.engine.dispose()


def test_scene_lifecycle_copy_snapshot_and_reference_confirmation(scene_scope) -> None:
    database, admin, user_scope, _objects, _storage, service = scene_scope
    media = service.store_system_media(
        admin,
        content=b"\x89PNG\r\n\x1a\nvalid-cover",
        mime_type="image/png",
        filename="cover.png",
        reason="运营上传系统场景封面",
    )
    created = service.create(
        admin,
        _payload(cover_media_id=media.media_id),
        reason="运营创建首批系统场景",
    )
    assert created.record.user_id is None
    assert created.record.workspace_id is None
    assert service.list_enabled(user_scope.identity)[0].record.id == created.record.id

    copied = service.copy_to_workspace(user_scope, created.record.id)
    assert copied.scope == "workspace"
    assert copied.provenance["source_version"] == 1
    assert copied.provenance["source_snapshot"]["name"] == "雨夜街巷"

    with pytest.raises(SystemSceneConflictError) as referenced:
        service.update(
            admin,
            created.record.id,
            created.payload.model_copy(update={"visibility": "disabled"}),
            expected_version=1,
            reason="运营下架已被引用的场景",
        )
    assert referenced.value.code == "SYSTEM_SCENE_REFERENCED"
    assert referenced.value.details == {"usage_count": 1}

    disabled = service.update(
        admin,
        created.record.id,
        created.payload.model_copy(update={"visibility": "disabled"}),
        expected_version=1,
        confirmed_usage_count=1,
        reason="运营确认引用数量后下架场景",
    )
    assert disabled.record.version == 2
    assert service.list_enabled(user_scope.identity) == []
    with pytest.raises(SystemSceneNotFoundError):
        service.get(user_scope.identity, created.record.id, admin_view=False)

    url, _expires_at = service.authorized_media_url(user_scope.identity, media.media_id)
    assert url.startswith("https://objects.example.test/access?")
    assert "lumenx/system/scenes" not in url
    with database.session_factory() as session:
        persisted_copy = session.get(AssetRecord, copied.id)
        assert persisted_copy.provenance["source_snapshot"]["visibility"] == "enabled"

    with pytest.raises(SystemSceneConflictError) as stale:
        service.update(
            admin,
            created.record.id,
            disabled.payload,
            expected_version=1,
            reason="运营使用过期版本修改场景",
        )
    assert stale.value.code == "SYSTEM_SCENE_VERSION_CONFLICT"

    archived = service.update(
        admin,
        created.record.id,
        disabled.payload,
        expected_version=2,
        confirmed_usage_count=1,
        archive=True,
        reason="运营确认引用数量后归档场景",
    )
    assert archived.record.deleted_at is not None
    with database.session_factory() as session:
        persisted_copy = session.get(AssetRecord, copied.id)
        assert persisted_copy.deleted_at is None
        assert persisted_copy.provenance["source_version"] == 1


def test_scene_seed_is_idempotent_and_payload_media_validation_is_strict(scene_scope) -> None:
    _database, admin, _user_scope, _objects, _storage, service = scene_scope
    seeded = service.seed(admin, [("default-rainy-city", _payload(visibility="disabled"))])
    replayed = service.seed(admin, [("default-rainy-city", _payload(name="不应覆盖原场景"))])
    assert replayed[0].record.id == seeded[0].record.id
    assert replayed[0].payload.name == "雨夜街巷"

    with pytest.raises(MediaValidationError):
        service.store_system_media(
            admin,
            content=b"not-an-image",
            mime_type="image/png",
            filename="fake.png",
            reason="运营上传系统场景封面",
        )

    with pytest.raises(ValueError):
        _payload(name="English only")
    with pytest.raises(AdminAuthorizationError):
        service.create(
            _user_scope.identity,
            _payload(),
            reason="普通用户尝试创建系统场景",
        )


def test_system_scene_api_enforces_admin_csrf_and_user_read_only_copy(scene_scope) -> None:
    database, admin, user_scope, object_store, storage, service = scene_scope
    admin_principal = AdminSessionPrincipal(
        admin_id=int(admin.admin_id),
        session_id=int(admin.session_id or "1"),
        username="admin",
        must_change_password=False,
    )
    admin_sessions = FakeSessions(admin_principal)
    sessions = FakeSessions(
        SessionPrincipal(
            user_id=int(user_scope.identity.user_id),
            session_id=int(user_scope.identity.session_id or "1"),
            phone_canonical="+8613900139000",
            phone_verified=False,
        )
    )
    app = FastAPI()
    install_cloud_system_scenes_api(
        app,
        SimpleNamespace(database=database, sessions=sessions, admin_sessions=admin_sessions),
        DeploymentSettings(_env_file=None),
        storage,
    )

    @app.exception_handler(AdminAuthorizationError)
    def denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(status_code=403, content={"code": "ADMIN_REQUIRED", "message": str(exc)})

    @app.exception_handler(SessionAuthenticationError)
    def unauthenticated(_request: Request, exc: SessionAuthenticationError):
        return JSONResponse(status_code=401, content={"code": exc.code, "message": str(exc)})

    client = TestClient(app)
    client.cookies.set("lumenx_admin_session", "admin-session-token")
    client.cookies.set("lumenx_session", "session-token")
    created = client.post(
        "/admin/system-scenes",
        headers={"X-CSRF-Token": "csrf-token"},
        json={
            "scene": _payload().model_dump(mode="json"),
            "reason": "运营通过后台创建系统场景",
        },
    )
    assert created.status_code == 201
    assert created.json()["name"] == "雨夜街巷"
    assert admin_sessions.calls[-1] == ("admin-session-token", "csrf-token")

    catalog = client.get("/system-scenes")
    assert catalog.status_code == 200
    assert catalog.json()["items"][0]["usage_count"] is None
    assert "object_key" not in catalog.text

    client.cookies.delete("lumenx_admin_session")
    denied_admin = client.get("/admin/system-scenes")
    assert denied_admin.status_code == 401
    assert "雨夜街巷" not in denied_admin.text
    copied = client.post(
        f"/system-scenes/{created.json()['id']}/copy",
        headers={"X-Workspace-ID": user_scope.workspace_id, "X-CSRF-Token": "csrf-token"},
        json={},
    )
    assert copied.status_code == 201
    assert copied.json()["scope"] == "workspace"
    assert copied.json()["source_version"] == 1
    assert object_store.objects == {}
    client.close()


def test_admin_scene_filters_are_stable_and_media_validation_is_safe(scene_scope) -> None:
    database, admin, _user_scope, _object_store, storage, service = scene_scope
    first = service.create(admin, _payload(), reason="运营创建城市夜景场景")
    second = service.create(
        admin,
        _payload(
            name="清晨山谷",
            description="晨雾笼罩的绿色山谷",
            category="自然",
            tags=["清晨", "山谷"],
            schema_version=2,
            visibility="disabled",
            sort_order=20,
        ),
        reason="运营创建自然风景场景",
    )

    assert service.list_admin(admin, scene_id=first.record.id).items[0].record.id == first.record.id
    assert service.list_admin(admin, category="自然").items[0].record.id == second.record.id
    assert service.list_admin(admin, tag="山谷").items[0].record.id == second.record.id
    assert service.list_admin(admin, schema_version=2).items[0].record.id == second.record.id
    assert service.list_admin(admin, visibility="disabled").items[0].record.id == second.record.id

    principal = AdminSessionPrincipal(
        admin_id=int(admin.admin_id),
        session_id=int(admin.session_id or "1"),
        username="admin",
        must_change_password=False,
    )
    admin_sessions = FakeSessions(principal)
    sessions = FakeSessions(
        SessionPrincipal(
            user_id=2001,
            session_id=2101,
            phone_canonical="+8613900139000",
            phone_verified=False,
        )
    )
    app = FastAPI()
    install_cloud_system_scenes_api(
        app,
        SimpleNamespace(database=database, sessions=sessions, admin_sessions=admin_sessions),
        DeploymentSettings(_env_file=None),
        storage,
    )
    client = TestClient(app)
    client.cookies.set("lumenx_admin_session", "admin-session-token")
    invalid_media = client.post(
        "/admin/system-scenes/media",
        headers={"X-CSRF-Token": "csrf-token"},
        data={"reason": "运营上传场景封面"},
        files={"file": ("fake.png", b"not-an-image", "image/png")},
    )
    assert invalid_media.status_code == 422
    assert invalid_media.json()["code"] == "SYSTEM_MEDIA_INVALID"
    assert "not-an-image" not in invalid_media.text
    invalid_payload = client.post(
        "/admin/system-scenes",
        headers={"X-CSRF-Token": "csrf-token"},
        json={"scene": {**_payload().model_dump(mode="json"), "name": "English"}, "reason": "运营创建系统场景"},
    )
    assert invalid_payload.status_code == 422
    client.close()
