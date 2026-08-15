from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.sessions import SessionPrincipal
from src.platform.content_api import install_cloud_content_api
from src.platform.db_models import WorkspaceRecord
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor


class FakeSessions:
    def __init__(self, principal: SessionPrincipal) -> None:
        self.principal = principal
        self.calls: list[tuple[str, str | None]] = []

    def resolve(self, token: str, *, csrf_token: str | None = None) -> SessionPrincipal:
        self.calls.append((token, csrf_token))
        return self.principal


@pytest.fixture
def content_api():
    database = RepositoryDatabase()
    context = _create_scope(database)
    principal = SessionPrincipal(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
        is_platform_admin=False,
    )
    sessions = FakeSessions(principal)
    auth = SimpleNamespace(database=database, sessions=sessions)
    app = FastAPI()
    install_cloud_content_api(
        app,
        auth,
        script_processor=FakeScriptProcessor(),
    )

    @app.get("/projects/")
    def legacy_unscoped_project_list():
        return [{"source": "legacy"}]

    client = TestClient(app)
    client.cookies.set("lumenx_session", "session-token")
    yield database, context, sessions, client
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


def test_cloud_project_routes_require_scope_and_isolate_workspaces(content_api) -> None:
    database, context, sessions, client = content_api

    missing_scope = client.get("/projects/")
    assert missing_scope.status_code == 400
    assert missing_scope.json()["code"] == "WORKSPACE_CONTEXT_INVALID"

    created = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第一集", "text": "正文", "workflow_mode": "r2v"},
        headers=_headers(context.workspace_id),
    )
    assert created.status_code == 201
    assert created.json()["version"] == 1
    assert sessions.calls[-1] == ("session-token", "csrf-token")

    with database.session_factory.begin() as session:
        workspace = WorkspaceRecord(
            user_id=int(context.identity.user_id),
            name="其他工作区",
        )
        session.add(workspace)
        session.flush()
        other_workspace_id = workspace.id
    isolated_list = client.get(
        "/projects/",
        headers=_headers(str(other_workspace_id)),
    )
    assert isolated_list.status_code == 200
    assert isolated_list.json() == []

    model_override = client.post(
        "/projects?skip_analysis=true",
        json={
            "title": "越权模型项目",
            "text": "正文",
            "model": "client-selected-model",
        },
        headers=_headers(context.workspace_id),
    )
    assert model_override.status_code == 422


def test_cloud_project_mutations_require_current_if_match(content_api) -> None:
    _database, context, _sessions, client = content_api
    created = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第一集", "text": "正文"},
        headers=_headers(context.workspace_id),
    ).json()
    project_id = created["id"]

    missing_version = client.put(
        f"/projects/{project_id}/text",
        json={"text": "新正文"},
        headers=_headers(context.workspace_id),
    )
    assert missing_version.status_code == 428
    assert missing_version.json()["code"] == "CONTENT_VERSION_REQUIRED"

    updated = client.put(
        f"/projects/{project_id}/text",
        json={"text": "新正文"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert updated.status_code == 200
    assert updated.json()["original_text"] == "新正文"
    assert updated.json()["version"] == 2

    stale = client.put(
        f"/projects/{project_id}/text",
        json={"text": "旧页面正文"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONTENT_VERSION_CONFLICT"


def test_cloud_series_episode_routes_persist_both_sides(content_api) -> None:
    _database, context, _sessions, client = content_api
    series = client.post(
        "/series",
        json={"title": "第一季"},
        headers=_headers(context.workspace_id),
    ).json()
    project = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第一集", "text": "正文"},
        headers=_headers(context.workspace_id),
    ).json()

    bound = client.post(
        f"/series/{series['id']}/episodes",
        json={"script_id": project["id"], "episode_number": 2},
        headers=_headers(context.workspace_id, version=series["version"]),
    )
    assert bound.status_code == 200
    assert bound.json()["episode_ids"] == [project["id"]]
    assert bound.json()["version"] == 2

    stored_project = client.get(
        f"/projects/{project['id']}",
        headers=_headers(context.workspace_id),
    )
    assert stored_project.json()["series_id"] == series["id"]
    assert stored_project.json()["episode_number"] == 2

    detail = client.get(
        f"/series/{series['id']}",
        headers=_headers(context.workspace_id),
    )
    assert detail.status_code == 200
    assert detail.json()["episodes"][0]["id"] == project["id"]


def test_cloud_prompt_and_art_direction_routes_persist_versions(content_api) -> None:
    _database, context, _sessions, client = content_api
    project = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第一集", "text": "正文"},
        headers=_headers(context.workspace_id),
    ).json()

    prompt_update = client.put(
        f"/projects/{project['id']}/prompt_config",
        json={"style_analysis": "只推荐写实风格"},
        headers=_headers(context.workspace_id, version=project["version"]),
    )
    assert prompt_update.status_code == 200
    assert prompt_update.json()["prompt_config"]["style_analysis"] == "只推荐写实风格"
    assert prompt_update.json()["version"] == 2

    art_update = client.post(
        f"/projects/{project['id']}/art_direction/save",
        json={
            "selected_style_id": "cinematic",
            "style_config": {"name": "电影感"},
        },
        headers=_headers(context.workspace_id, version=2),
    )
    assert art_update.status_code == 200
    assert art_update.json()["art_direction"]["selected_style_id"] == "cinematic"
    assert art_update.json()["version"] == 3
