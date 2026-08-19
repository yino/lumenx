from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.apps.comic_gen.models import Character, Prop, Scene
from src.platform.asset_repositories import (
    PostgresProjectAssetRepository,
    PostgresSeriesAssetRepository,
)
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


def test_cloud_reconcile_routes_match_and_move_episode_assets(content_api) -> None:
    database, context, _sessions, client = content_api
    series = client.post(
        "/series",
        json={"title": "第一季"},
        headers=_headers(context.workspace_id),
    ).json()
    project = client.post(
        "/projects?skip_analysis=true",
        json={
            "title": "第二集",
            "text": "正文",
            "series_id": series["id"],
        },
        headers=_headers(context.workspace_id),
    ).json()

    project_assets = PostgresProjectAssetRepository(database)
    series_assets = PostgresSeriesAssetRepository(database)
    local_character = project_assets.add(
        context,
        project["id"],
        "character",
        Character(id="local-character", name="林骁", description="本集角色"),
    )
    local_scene = project_assets.add(
        context,
        project["id"],
        "scene",
        Scene(id="local-scene", name="出租屋", description="本集场景"),
    )
    local_prop = project_assets.add(
        context,
        project["id"],
        "prop",
        Prop(id="local-prop", name="戒指", description="本集道具"),
    )
    shared_character = series_assets.add(
        context,
        series["id"],
        "character",
        Character(id="shared-character", name="林骁", description="系列角色"),
    )

    suggestions = client.get(
        f"/projects/{project['id']}/reconcile/suggestions",
        headers=_headers(context.workspace_id),
    )

    assert suggestions.status_code == 200
    assert suggestions.json()["characters"] == [
        {
            "local_id": local_character.domain_id,
            "local_name": "林骁",
            "suggested_series_id": shared_character.domain_id,
            "suggested_series_name": "林骁",
            "confidence": 100,
        }
    ]
    assert suggestions.json()["scenes"][0]["confidence"] == 0
    assert suggestions.json()["props"][0]["suggested_series_id"] is None

    applied = client.post(
        f"/projects/{project['id']}/reconcile/apply",
        json={
            "characters": [
                {
                    "local_id": local_character.domain_id,
                    "action": "merge_into_series",
                    "target_series_id": shared_character.domain_id,
                }
            ],
            "scenes": [
                {
                    "local_id": local_scene.domain_id,
                    "action": "create_new_in_series",
                }
            ],
            "props": [
                {
                    "local_id": local_prop.domain_id,
                    "action": "skip",
                }
            ],
        },
        headers=_headers(context.workspace_id, version=project["version"]),
    )

    assert applied.status_code == 200
    assert applied.json()["version"] == project["version"] + 1
    assert {
        asset.domain_id for asset in project_assets.list(context, project["id"])
    } == {local_prop.domain_id}
    assert {
        asset.domain_id for asset in series_assets.list(context, series["id"])
    } == {shared_character.domain_id, local_scene.domain_id}

    stale = client.post(
        f"/projects/{project['id']}/reconcile/apply",
        json={"characters": [], "scenes": [], "props": []},
        headers=_headers(context.workspace_id, version=project["version"]),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONTENT_VERSION_CONFLICT"


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


def test_next_episode_hook_uses_scoped_project_state_and_optimistic_versions(
    content_api,
) -> None:
    database, context, _sessions, client = content_api
    project = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第一集", "text": "旧结尾"},
        headers=_headers(context.workspace_id),
    ).json()
    project_id = project["id"]

    initial = client.get(
        f"/projects/{project_id}/next_hook",
        headers=_headers(context.workspace_id),
    )
    saved = client.put(
        f"/projects/{project_id}/next_hook",
        json={"hook": "下一集从雨夜追逐开始。"},
        headers=_headers(context.workspace_id, version=project["version"]),
    )
    stale_write = client.put(
        f"/projects/{project_id}/next_hook",
        json={"hook": "旧页面覆盖"},
        headers=_headers(context.workspace_id, version=project["version"]),
    )
    text_updated = client.put(
        f"/projects/{project_id}/text",
        json={"text": "已经改写的新结尾"},
        headers=_headers(context.workspace_id, version=saved.json()["version"]),
    )
    stale_state = client.get(
        f"/projects/{project_id}/next_hook",
        headers=_headers(context.workspace_id),
    )
    cleared = client.put(
        f"/projects/{project_id}/next_hook",
        json={"hook": None},
        headers=_headers(context.workspace_id, version=text_updated.json()["version"]),
    )

    assert initial.status_code == 200
    assert initial.json() == {
        "has_text": True,
        "hook": None,
        "stale": False,
        "version": 1,
    }
    assert saved.status_code == 200
    assert saved.json() == {
        "hook": "下一集从雨夜追逐开始。",
        "stale": False,
        "version": 2,
    }
    assert stale_write.status_code == 409
    assert stale_write.json()["code"] == "CONTENT_VERSION_CONFLICT"
    assert stale_state.json() == {
        "has_text": True,
        "hook": "下一集从雨夜追逐开始。",
        "stale": True,
        "version": 3,
    }
    assert cleared.json() == {"hook": None, "stale": False, "version": 4}

    other_context = _create_scope(database)
    isolated = client.get(
        f"/projects/{project_id}/next_hook",
        headers=_headers(other_context.workspace_id),
    )
    assert isolated.status_code == 404
    assert isolated.json()["code"] == "CONTENT_NOT_FOUND"


def test_previous_episode_summary_reads_series_and_tracks_previous_revision(
    content_api,
) -> None:
    _database, context, _sessions, client = content_api
    standalone = client.post(
        "/projects?skip_analysis=true",
        json={"title": "独立项目", "text": "正文"},
        headers=_headers(context.workspace_id),
    ).json()
    no_previous = client.get(
        f"/projects/{standalone['id']}/previous_episode",
        headers=_headers(context.workspace_id),
    )
    assert no_previous.json() == {
        "has_previous": False,
        "previous_episode_id": None,
        "previous_episode_title": None,
        "raw_snippet": "",
        "ai_summary": None,
        "ai_summary_stale": False,
        "last_frames": [],
        "version": standalone["version"],
    }

    series = client.post(
        "/series",
        json={"title": "第一季"},
        headers=_headers(context.workspace_id),
    ).json()
    previous = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第一集", "text": "上一集正文", "series_id": series["id"]},
        headers=_headers(context.workspace_id),
    ).json()
    current = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第二集", "text": "本集正文", "series_id": series["id"]},
        headers=_headers(context.workspace_id),
    ).json()

    initial = client.get(
        f"/projects/{current['id']}/previous_episode",
        headers=_headers(context.workspace_id),
    )
    saved = client.put(
        f"/projects/{current['id']}/last_episode_summary",
        json={"ai_summary": "上一集留下了门后的悬念。"},
        headers=_headers(context.workspace_id, version=current["version"]),
    )
    previous_updated = client.put(
        f"/projects/{previous['id']}/text",
        json={"text": "上一集已经改写"},
        headers=_headers(context.workspace_id, version=previous["version"]),
    )
    outdated_generated_summary = client.put(
        f"/projects/{current['id']}/last_episode_summary",
        json={
            "ai_summary": "基于旧剧本生成的摘要",
            "source_previous_version": previous["version"],
        },
        headers=_headers(context.workspace_id, version=saved.json()["version"]),
    )
    stale = client.get(
        f"/projects/{current['id']}/previous_episode",
        headers=_headers(context.workspace_id),
    )

    assert initial.status_code == 200
    assert initial.json() == {
        "has_previous": True,
        "previous_episode_id": previous["id"],
        "previous_episode_title": "第一集",
        "raw_snippet": "上一集正文",
        "ai_summary": None,
        "ai_summary_stale": False,
        "last_frames": [],
        "version": current["version"],
    }
    assert saved.status_code == 200
    assert saved.json() == {
        "ai_summary": "上一集留下了门后的悬念。",
        "ai_summary_stale": False,
        "previous_episode_id": previous["id"],
        "previous_episode_title": "第一集",
        "version": 2,
    }
    assert previous_updated.status_code == 200
    assert outdated_generated_summary.status_code == 409
    assert outdated_generated_summary.json() == {
        "code": "CONTENT_VERSION_CONFLICT",
        "message": "上一集内容已更新，请重新生成摘要",
    }
    assert stale.json()["ai_summary"] == "上一集留下了门后的悬念。"
    assert stale.json()["ai_summary_stale"] is True
    assert stale.json()["raw_snippet"] == "上一集已经改写"
