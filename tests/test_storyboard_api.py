from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.apps.comic_gen.models import StoryboardFrame
from src.platform.auth.sessions import SessionPrincipal
from src.platform.content_repositories import PostgresProjectRepository
from src.platform.db_models import WorkspaceRecord
from src.platform.media_api import install_cloud_media_api
from src.platform.settings import DeploymentSettings
from src.platform.storyboard_api import install_cloud_storyboard_api
from tests.test_content_api import FakeSessions
from tests.test_content_repositories import RepositoryDatabase, _create_scope, _script
from tests.test_media_storage import FakePrivateObjectStore


@pytest.fixture
def storyboard_api():
    database = RepositoryDatabase()
    context = _create_scope(database)
    project = PostgresProjectRepository(database).add(
        context,
        _script(
            frames=[
                StoryboardFrame(
                    id="frame-1",
                    scene_id="scene-1",
                    action_description="人物走进房间",
                    rendered_image_asset=None,
                )
            ]
        ),
    )
    other_project = PostgresProjectRepository(database).add(
        context,
        _script(title="第二集"),
    )
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
    install_cloud_storyboard_api(app, auth, media_storage)

    @app.post("/projects/{project_id}/generate_video")
    def legacy_generate_video(project_id: str):
        return {"source": "legacy", "project_id": project_id}

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


def _headers(workspace_id: str, *, version: int | None = None) -> dict[str, str]:
    headers = {
        "X-Workspace-ID": workspace_id,
        "X-CSRF-Token": "csrf-token",
    }
    if version is not None:
        headers["If-Match"] = str(version)
    return headers


def test_cloud_storyboard_routes_persist_with_scope_and_versions(
    storyboard_api,
) -> None:
    database, context, sessions, _store, client, project_id, _other = storyboard_api

    missing_version = client.post(
        f"/projects/{project_id}/frames",
        json={"action_description": "新增分镜"},
        headers=_headers(context.workspace_id),
    )
    assert missing_version.status_code == 428

    created = client.post(
        f"/projects/{project_id}/frames",
        json={"action_description": "新增分镜", "scene_id": "scene-2"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert created.status_code == 201
    assert created.json()["version"] == 2
    created_frame_id = created.json()["frames"][-1]["id"]

    updated = client.post(
        f"/projects/{project_id}/frames/update",
        json={
            "frame_id": created_frame_id,
            "dialogue": "开始吧",
            "camera_movement_description": "缓慢推进",
        },
        headers=_headers(context.workspace_id, version=2),
    )
    assert updated.status_code == 200
    assert updated.json()["frames"][-1]["camera_movement"] == "缓慢推进"
    assert sessions.calls[-1] == ("session-token", "csrf-token")

    unsafe_workbench_paths = client.patch(
        f"/projects/{project_id}/frames/{created_frame_id}/workbench",
        json={"t2i_image_urls": ["/files/forged.png"]},
        headers=_headers(context.workspace_id, version=3),
    )
    assert unsafe_workbench_paths.status_code == 422

    stale = client.post(
        f"/projects/{project_id}/frames/toggle_lock",
        json={"frame_id": created_frame_id},
        headers=_headers(context.workspace_id, version=1),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONTENT_VERSION_CONFLICT"

    with database.session_factory.begin() as session:
        workspace = WorkspaceRecord(
            user_id=int(context.identity.user_id),
            name="其他工作区",
        )
        session.add(workspace)
        session.flush()
        other_workspace_id = workspace.id
    hidden = client.post(
        f"/projects/{project_id}/frames/toggle_lock",
        json={"frame_id": created_frame_id},
        headers=_headers(str(other_workspace_id), version=3),
    )
    assert hidden.status_code == 404


def test_cloud_storyboard_uploads_store_private_media_ids(storyboard_api) -> None:
    _database, context, _sessions, object_store, client, project_id, _other = (
        storyboard_api
    )

    rendered = client.post(
        f"/projects/{project_id}/frames/frame-1/upload_image",
        files={"file": ("render.png", b"rendered-image", "image/png")},
        headers=_headers(context.workspace_id, version=1),
    )
    assert rendered.status_code == 200
    assert rendered.json()["version"] == 2
    reference = rendered.json()["frames"][0]["rendered_image_url"]
    assert reference.startswith("media:")
    assert "/files/" not in rendered.text
    assert "object_key" not in rendered.text

    t2i = client.post(
        f"/projects/{project_id}/frames/frame-1/upload_t2i",
        files={"file": ("first.webp", b"first-frame", "image/webp")},
        headers=_headers(context.workspace_id, version=2),
    )
    assert t2i.status_code == 200
    assert t2i.json()["version"] == 3
    assert t2i.json()["image_url"].startswith("media:")
    assert len(object_store.objects) == 2
    assert all(
        key.startswith(
            f"users/{context.identity.user_id}/workspaces/"
            f"{context.workspace_id}/projects/{project_id}/"
        )
        for key in object_store.objects
    )

    invalid_type = client.post(
        f"/projects/{project_id}/frames/frame-1/upload_t2i",
        files={"file": ("payload.svg", b"<svg/>", "image/svg+xml")},
        headers=_headers(context.workspace_id, version=3),
    )
    assert invalid_type.status_code == 422
    assert invalid_type.json()["code"] == "MEDIA_INVALID"


def test_cloud_storyboard_media_and_audio_mix_reject_path_authority(
    storyboard_api,
) -> None:
    _database, context, _sessions, _store, client, project_id, other_project_id = (
        storyboard_api
    )
    shared_media_id = client.post(
        "/media",
        files={"file": ("music.mp3", b"music", "audio/mpeg")},
        headers=_headers(context.workspace_id),
    ).json()["id"]
    foreign_project_media_id = client.post(
        f"/media?project_id={other_project_id}",
        files={"file": ("other.mp3", b"other", "audio/mpeg")},
        headers=_headers(context.workspace_id),
    ).json()["id"]

    raw_path = client.put(
        f"/projects/{project_id}/audio_mix",
        json={"bgm_url": "/files/music.mp3"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert raw_path.status_code == 422

    updated = client.put(
        f"/projects/{project_id}/audio_mix",
        json={"background_music_media_id": shared_media_id, "bgm_volume": 25},
        headers=_headers(context.workspace_id, version=1),
    )
    assert updated.status_code == 200
    assert updated.json()["bgm_url"] == f"media:{shared_media_id}"
    assert updated.json()["mix_settings"]["bgm"] == 25

    foreign_media = client.post(
        f"/projects/{project_id}/frames/frame-1/media",
        json={"media_kind": "audio", "media_id": foreign_project_media_id},
        headers=_headers(context.workspace_id, version=2),
    )
    assert foreign_media.status_code == 404


def test_cloud_ai_media_routes_never_reach_legacy_pipeline(storyboard_api) -> None:
    _database, context, _sessions, _store, client, project_id, _other = storyboard_api

    blocked = client.post(
        f"/projects/{project_id}/generate_video",
        headers=_headers(context.workspace_id),
    )
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "AI_GATEWAY_NOT_READY"

    model_override = client.post(
        f"/projects/{project_id}/generate_video",
        json={"provider": "client-provider", "model": "client-model"},
        headers=_headers(context.workspace_id),
    )
    assert model_override.status_code == 422
    assert model_override.json()["detail"]["code"] == "AI_OVERRIDE_FORBIDDEN"

    bgm_catalog = client.get(
        "/bgm/presets",
        headers=_headers(context.workspace_id),
    )
    assert bgm_catalog.status_code == 503
    assert bgm_catalog.json()["detail"]["code"] == "MEDIA_CATALOG_NOT_READY"
