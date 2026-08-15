from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.asset_repositories import PostgresWorkspaceAssetRepository
from src.platform.auth.sessions import SessionPrincipal
from src.platform.db_models import AITaskRecord, WorkspaceRecord
from src.platform.media_api import install_cloud_media_api
from src.platform.playground_api import install_cloud_playground_api
from src.platform.settings import DeploymentSettings
from tests.test_content_api import FakeSessions
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_media_storage import FakePrivateObjectStore


@pytest.fixture
def playground_api():
    database = RepositoryDatabase()
    AITaskRecord.__table__.create(database.engine)
    context = _create_scope(database)
    other_context = _create_scope(database)
    principal = SessionPrincipal(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
        is_platform_admin=False,
    )
    sessions = FakeSessions(principal)
    auth = SimpleNamespace(database=database, sessions=sessions)
    object_store = FakePrivateObjectStore()
    app = FastAPI()
    media_storage = install_cloud_media_api(
        app,
        auth,
        DeploymentSettings(),
        object_store=object_store,
    )
    install_cloud_playground_api(app, auth, media_storage)

    @app.post("/playground/generate")
    def legacy_generate():
        return {"source": "legacy"}

    client = TestClient(app)
    client.cookies.set("lumenx_session", "session-token")
    yield database, context, other_context, sessions, object_store, client
    client.close()
    database.engine.dispose()


def _headers(workspace_id: str, *, version: int | None = None) -> dict[str, str]:
    headers = {
        "X-Workspace-ID": workspace_id,
        "X-CSRF-Token": "csrf-token",
    }
    if version is not None:
        headers["If-Match"] = str(version)
    return headers


def _add_task(
    database: RepositoryDatabase,
    *,
    user_id: str,
    workspace_id: str,
    media_id: str,
    prompt: str,
) -> str:
    with database.session_factory.begin() as session:
        task = AITaskRecord(
            user_id=int(user_id),
            workspace_id=int(workspace_id),
            capability="playground.image",
            status="succeeded",
            idempotency_key=str(uuid.uuid4()),
            request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            request_payload={
                "mode": "t2i",
                "prompt": prompt,
                "input_media_ids": [],
                "parameters": {"size": "1024x1024"},
                "batch_size": 1,
            },
            config_snapshot={
                "display_name": "平台图像模型",
                "provider_model_id": "provider-image-v1",
            },
            tokens_per_ticket=1000,
            quoted_microtickets=1000000,
            result={
                "outputs": [
                    {
                        "id": "output-1",
                        "media_id": media_id,
                        "media_type": "image",
                    }
                ]
            },
        )
        session.add(task)
        session.flush()
        task_id = task.id
    return str(task_id)


def test_cloud_playground_templates_are_workspace_scoped(playground_api) -> None:
    database, context, _other_context, sessions, _store, client = playground_api

    model_override = client.post(
        "/playground/templates",
        json={
            "name": "不安全模板",
            "prompt": "电影感画面",
            "default_model_id": "user-selected-model",
        },
        headers=_headers(context.workspace_id),
    )
    assert model_override.status_code == 422

    nested_model_override = client.post(
        "/playground/templates",
        json={
            "name": "不安全参数模板",
            "prompt": "电影感画面",
            "default_parameters": {"model": "user-selected-model"},
        },
        headers=_headers(context.workspace_id),
    )
    assert nested_model_override.status_code == 422
    assert nested_model_override.json()["code"] == "PLAYGROUND_INVALID"

    created = client.post(
        "/playground/templates",
        json={"name": "电影画面", "prompt": "电影感画面", "default_mode": "t2i"},
        headers=_headers(context.workspace_id),
    )
    assert created.status_code == 201
    assert created.json()["version"] == 1
    assert "default_model_id" not in created.json()
    template_id = created.json()["id"]

    listed = client.get(
        "/playground/templates",
        headers=_headers(context.workspace_id),
    )
    assert [item["id"] for item in listed.json()] == [template_id]
    assert sessions.calls[-1] == ("session-token", None)

    with database.session_factory.begin() as session:
        workspace = WorkspaceRecord(
            user_id=int(context.identity.user_id),
            name="其他工作区",
        )
        session.add(workspace)
        session.flush()
        other_workspace_id = workspace.id
    isolated = client.get(
        "/playground/templates",
        headers=_headers(str(other_workspace_id)),
    )
    assert isolated.status_code == 200
    assert isolated.json() == []

    updated = client.put(
        f"/playground/templates/{template_id}",
        json={"name": "电影分镜"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "电影分镜"
    assert updated.json()["version"] == 2

    stale = client.delete(
        f"/playground/templates/{template_id}",
        headers=_headers(context.workspace_id, version=1),
    )
    assert stale.status_code == 409

    deleted = client.delete(
        f"/playground/templates/{template_id}",
        headers=_headers(context.workspace_id, version=2),
    )
    assert deleted.status_code == 200


def test_cloud_playground_upload_returns_media_id_not_path(playground_api) -> None:
    _database, context, _other, _sessions, object_store, client = playground_api

    uploaded = client.post(
        "/playground/upload",
        files={"file": ("reference.png", b"image-content", "image/png")},
        headers=_headers(context.workspace_id),
    )
    assert uploaded.status_code == 201
    assert set(uploaded.json()) == {
        "id",
        "mime_type",
        "size_bytes",
        "checksum_sha256",
    }
    assert "path" not in uploaded.json()
    assert len(object_store.objects) == 1
    assert next(iter(object_store.objects)).startswith(
        f"users/{context.identity.user_id}/workspaces/{context.workspace_id}/"
        "projects/shared/"
    )


def test_cloud_playground_history_polling_and_saved_assets_are_scoped(
    playground_api,
) -> None:
    database, context, other_context, _sessions, _store, client = playground_api
    media_id = client.post(
        "/playground/upload",
        files={"file": ("result.png", b"result", "image/png")},
        headers=_headers(context.workspace_id),
    ).json()["id"]
    task_id = _add_task(
        database,
        user_id=context.identity.user_id,
        workspace_id=context.workspace_id,
        media_id=media_id,
        prompt="霓虹雨夜",
    )
    _add_task(
        database,
        user_id=other_context.identity.user_id,
        workspace_id=other_context.workspace_id,
        media_id=media_id,
        prompt="不可见内容",
    )

    history = client.get(
        "/playground/history",
        headers=_headers(context.workspace_id),
    )
    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == [task_id]
    assert history.json()[0]["outputs"] == [
        {
            "id": "output-1",
            "media_id": media_id,
            "media_type": "image",
            "saved_asset_id": None,
        }
    ]
    assert "media_path" not in history.text
    assert history.json()[0]["actual_model_id"] == "provider-image-v1"
    assert history.json()[0]["actual_model_name"] == "平台图像模型"
    assert history.json()[0]["quoted_microtickets"] == "1000000"
    assert history.json()[0]["quoted_tickets"] == "1"
    assert history.json()[0]["tokens_per_ticket"] == "1000"
    assert history.json()[0]["raw_status"] == "succeeded"
    assert history.json()[0]["status_zh"] == "已完成"
    assert history.json()[0]["cancellation_requested"] is False
    assert history.json()[0]["support_review"] is False

    status = client.get(
        f"/playground/history/{task_id}/status",
        headers=_headers(context.workspace_id),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["quoted_tickets"] == "1"
    assert status.json()["status_zh"] == "已完成"

    saved = client.post(
        f"/playground/history/{task_id}/outputs/output-1/save-to-library",
        json={"category": "scene"},
        headers=_headers(context.workspace_id),
    )
    assert saved.status_code == 200
    library = PostgresWorkspaceAssetRepository(database).list(context)
    assert any(asset.domain_id == saved.json()["asset_id"] for asset in library)
    assert all(asset.media_object_id == media_id for asset in library)

    hidden = client.delete(
        f"/playground/history/{task_id}",
        headers=_headers(context.workspace_id),
    )
    assert hidden.status_code == 200
    assert client.get(
        f"/playground/history/{task_id}",
        headers=_headers(context.workspace_id),
    ).status_code == 404


def test_cloud_playground_generate_never_reaches_local_background_task(
    playground_api,
) -> None:
    _database, context, _other, _sessions, _store, client = playground_api

    blocked = client.post(
        "/playground/generate",
        json={"model_id": "client-model", "prompt": "测试"},
        headers=_headers(context.workspace_id),
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "AI_OVERRIDE_FORBIDDEN"

    gateway_pending = client.post(
        "/playground/generate",
        json={"prompt": "测试", "parameters": {"duration": 5}},
        headers=_headers(context.workspace_id),
    )
    assert gateway_pending.status_code == 503
    assert gateway_pending.json()["detail"]["code"] == "AI_GATEWAY_NOT_READY"


def test_cloud_playground_projects_real_gateway_task_payload_and_media_results(
    playground_api,
) -> None:
    database, context, _other, _sessions, _store, client = playground_api
    media_id = client.post(
        "/playground/upload",
        files={"file": ("result.png", b"result", "image/png")},
        headers=_headers(context.workspace_id),
    ).json()["id"]
    with database.session_factory.begin() as session:
        task = AITaskRecord(
            user_id=int(context.identity.user_id),
            workspace_id=int(context.workspace_id),
            capability="image.t2i",
            status="succeeded",
            idempotency_key=str(uuid.uuid4()),
            request_fingerprint=uuid.uuid4().hex + uuid.uuid4().hex,
            request_payload={
                "content": {
                    "operation": "playground.t2i",
                    "mode": "t2i",
                    "prompt": "雨夜街道",
                    "batch_size": 1,
                },
                "input_media_ids": [],
                "parameters": {"size": "1024x1024"},
            },
            config_snapshot={
                "display_name": "平台图像模型",
                "provider_model_id": "provider-image-v1",
            },
            tokens_per_ticket=1000,
            quoted_microtickets=1000000,
            result={"media_ids": [media_id]},
        )
        session.add(task)
        session.flush()
        task_id = task.id

    response = client.get(
        f"/playground/history/{task_id}",
        headers=_headers(context.workspace_id),
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "t2i"
    assert response.json()["prompt"] == "雨夜街道"
    assert response.json()["outputs"] == [
        {
            "id": "media-1",
            "media_id": media_id,
            "media_type": "image",
            "saved_asset_id": None,
        }
    ]

    saved = client.post(
        f"/playground/history/{task_id}/outputs/media-1/save-to-library",
        json={"category": "scene"},
        headers=_headers(context.workspace_id),
    )
    assert saved.status_code == 200
    refreshed = client.get(
        f"/playground/history/{task_id}",
        headers=_headers(context.workspace_id),
    ).json()
    assert refreshed["outputs"][0]["saved_asset_id"] == saved.json()["asset_id"]
