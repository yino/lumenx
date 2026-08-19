from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.platform.asset_api import install_cloud_asset_api
from src.platform.auth.sessions import SessionPrincipal
from src.platform.content_service import CloudContentService
from src.platform.db_models import AuditEventRecord, WorkspaceRecord
from src.platform.media_api import install_cloud_media_api
from src.platform.settings import DeploymentSettings
from tests.test_asset_api import _headers
from tests.test_content_api import FakeSessions
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor
from tests.test_media_storage import FakePrivateObjectStore


@pytest.fixture
def media_api():
    database = RepositoryDatabase()
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)
    content = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    )
    project = content.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
    )
    other_project = content.create_project(
        context,
        title="第二集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
    )
    principal = SessionPrincipal(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
    )
    sessions = FakeSessions(principal)
    auth = SimpleNamespace(
        database=database,
        sessions=sessions,
        runtime_policy=SimpleNamespace(
            resolve=lambda: SimpleNamespace(
                config_version_id="media-policy-v1",
                signed_media_url_seconds=60,
            )
        ),
    )
    object_store = FakePrivateObjectStore()
    app = FastAPI()
    media_storage = install_cloud_media_api(
        app,
        auth,
        DeploymentSettings(),
        object_store=object_store,
    )
    install_cloud_asset_api(app, auth, media_storage=media_storage)
    client = TestClient(app)
    client.cookies.set("lumenx_session", "session-token")
    yield (
        database,
        context,
        sessions,
        object_store,
        client,
        project.document.id,
        other_project.document.id,
    )
    client.close()
    database.engine.dispose()


def test_media_upload_returns_id_without_object_key(media_api) -> None:
    (
        _database,
        context,
        sessions,
        object_store,
        client,
        project_id,
        _other_project_id,
    ) = media_api
    response = client.post(
        f"/media?project_id={project_id}",
        files={"file": ("avatar.png", b"image-content", "image/png")},
        headers=_headers(context.workspace_id),
    )

    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"id", "mime_type", "size_bytes", "checksum_sha256"}
    assert payload["mime_type"] == "image/png"
    assert len(object_store.objects) == 1
    assert sessions.calls[-1] == ("session-token", "csrf-token")

    metadata = client.get(
        f"/media/{payload['id']}",
        headers=_headers(context.workspace_id),
    )
    assert metadata.status_code == 200
    assert metadata.json()["project_id"] == project_id
    assert "object_key" not in metadata.json()

    access = client.get(
        f"/media/{payload['id']}/access?expires_seconds=120",
        headers=_headers(context.workspace_id),
    )
    assert access.status_code == 200
    assert access.json()["media_id"] == payload["id"]
    assert access.json()["url"].startswith("https://private.example/")
    assert access.json()["policy_config_version_id"] == "media-policy-v1"
    assert object_store.signed[-1][1] <= 60

    download = client.get(
        f"/media/{payload['id']}/download",
        headers=_headers(context.workspace_id),
    )
    assert download.status_code == 200
    assert download.content == b"image-content"
    assert download.headers["content-type"] == "image/png"
    assert download.headers["cache-control"] == "private, no-store"
    assert download.headers["content-disposition"].endswith('.png"')


def test_legacy_upload_paths_use_media_id_contract(media_api) -> None:
    (
        _database,
        context,
        _sessions,
        _object_store,
        client,
        project_id,
        _other_project_id,
    ) = media_api
    generic = client.post(
        f"/upload?project_id={project_id}",
        files={"file": ("frame.png", b"frame", "image/png")},
        headers=_headers(context.workspace_id),
    )
    library = client.post(
        "/library/assets/upload",
        files={"file": ("shared.png", b"shared", "image/png")},
        headers=_headers(context.workspace_id),
    )

    assert generic.status_code == 201
    assert library.status_code == 201
    assert "id" in generic.json()
    assert "id" in library.json()
    assert "path" not in generic.json()
    assert "image_url" not in library.json()


def test_media_access_and_delete_are_workspace_scoped(media_api) -> None:
    (
        database,
        context,
        _sessions,
        _object_store,
        client,
        project_id,
        _other_project_id,
    ) = media_api
    media_id = client.post(
        f"/media?project_id={project_id}",
        files={"file": ("avatar.png", b"image-content", "image/png")},
        headers=_headers(context.workspace_id),
    ).json()["id"]
    with database.session_factory.begin() as session:
        workspace = WorkspaceRecord(
            user_id=int(context.identity.user_id),
            name="其他工作区",
        )
        session.add(workspace)
        session.flush()
        other_workspace_id = workspace.id

    hidden = client.get(
        f"/media/{media_id}/access",
        headers=_headers(str(other_workspace_id)),
    )
    assert hidden.status_code == 404
    assert hidden.json()["message"] == "资源不存在"

    deleted = client.delete(
        f"/media/{media_id}",
        headers=_headers(context.workspace_id),
    )
    assert deleted.status_code == 200
    with database.session_factory() as session:
        event = session.scalar(
            select(AuditEventRecord).where(AuditEventRecord.action == "media.delete")
        )
        assert event is not None
        assert event.target_id == media_id
        assert event.workspace_id == int(context.workspace_id)
    unavailable = client.get(
        f"/media/{media_id}/access",
        headers=_headers(context.workspace_id),
    )
    assert unavailable.status_code == 404


def test_cloud_asset_media_fields_accept_only_owned_media_ids(media_api) -> None:
    (
        _database,
        context,
        _sessions,
        _object_store,
        client,
        project_id,
        other_project_id,
    ) = media_api
    project_media_id = client.post(
        f"/media?project_id={project_id}",
        files={"file": ("avatar.png", b"image-content", "image/png")},
        headers=_headers(context.workspace_id),
    ).json()["id"]
    other_media_id = client.post(
        f"/media?project_id={other_project_id}",
        files={"file": ("other.png", b"other-content", "image/png")},
        headers=_headers(context.workspace_id),
    ).json()["id"]

    raw_path = client.post(
        f"/projects/{project_id}/characters",
        json={
            "name": "不安全角色",
            "description": "测试",
            "image_url": "users/forged/object.png",
        },
        headers=_headers(context.workspace_id),
    )
    assert raw_path.status_code == 422

    created = client.post(
        f"/projects/{project_id}/characters",
        json={
            "name": "林墨",
            "description": "主角",
            "media_id": project_media_id,
        },
        headers=_headers(context.workspace_id),
    )
    assert created.status_code == 201
    assert created.json()["media_id"] == project_media_id
    assert created.json()["image_url"] == f"media:{project_media_id}"

    foreign_project_media = client.post(
        f"/projects/{project_id}/scenes",
        json={
            "name": "错误场景",
            "description": "测试",
            "media_id": other_media_id,
        },
        headers=_headers(context.workspace_id),
    )
    assert foreign_project_media.status_code == 404


def test_signed_url_expiry_is_bounded_by_api_validation(media_api) -> None:
    (
        _database,
        context,
        _sessions,
        _object_store,
        client,
        project_id,
        _other_project_id,
    ) = media_api
    media_id = client.post(
        f"/media?project_id={project_id}",
        files={"file": ("avatar.png", b"image-content", "image/png")},
        headers=_headers(context.workspace_id),
    ).json()["id"]

    response = client.get(
        f"/media/{media_id}/access?expires_seconds=901",
        headers=_headers(context.workspace_id),
    )
    assert response.status_code == 422


def test_project_asset_upload_creates_and_attaches_media_id(media_api) -> None:
    (
        _database,
        context,
        _sessions,
        _object_store,
        client,
        project_id,
        _other_project_id,
    ) = media_api
    character = client.post(
        f"/projects/{project_id}/characters",
        json={"name": "林墨", "description": "主角"},
        headers=_headers(context.workspace_id),
    ).json()

    uploaded = client.post(
        f"/projects/{project_id}/assets/character/{character['id']}/upload",
        files={"file": ("avatar.png", b"image-content", "image/png")},
        headers=_headers(context.workspace_id, version=character["version"]),
    )

    assert uploaded.status_code == 200
    assert uploaded.json()["media_id"] is not None
    assert uploaded.json()["image_url"] == f"media:{uploaded.json()['media_id']}"
    assert "object_key" not in uploaded.json()
