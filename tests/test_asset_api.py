from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.apps.comic_gen.models import Character
from src.platform.asset_api import install_cloud_asset_api
from src.platform.auth.sessions import SessionPrincipal
from src.platform.content_service import CloudContentService
from src.platform.contracts import MediaWrite
from src.platform.db_models import AssetRecord, WorkspaceRecord
from src.platform.media_storage import CloudMediaStorage
from tests.test_content_api import FakeSessions
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor
from tests.test_media_storage import FakePrivateObjectStore


@pytest.fixture
def asset_api():
    database = RepositoryDatabase()
    context = _create_scope(database)
    content = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    )
    source_series = content.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    target_series = content.create_series(
        context,
        title="第二季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    project = content.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
        series_id=source_series.document.id,
    )
    principal = SessionPrincipal(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
    )
    sessions = FakeSessions(principal)
    auth = SimpleNamespace(database=database, sessions=sessions)
    app = FastAPI()
    install_cloud_asset_api(app, auth)

    @app.post("/projects/{project_id}/characters")
    def legacy_character_create(project_id: str):
        return {"source": "legacy", "project_id": project_id}

    client = TestClient(app)
    client.cookies.set("lumenx_session", "session-token")
    yield (
        database,
        context,
        sessions,
        client,
        source_series.document.id,
        target_series.document.id,
        project.document.id,
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


def test_system_voice_catalog_is_available_without_ai_generation(asset_api) -> None:
    (
        _database,
        context,
        sessions,
        client,
        _source_series_id,
        _target_series_id,
        _project_id,
    ) = asset_api

    response = client.get("/voices", headers=_headers(context.workspace_id))

    assert response.status_code == 200
    voices = response.json()
    assert voices
    assert all(
        {
            "id",
            "name",
            "gender",
            "model",
            "family",
            "supports_instruction",
            "dialect",
            "lang_primary",
            "origin",
        }
        <= voice.keys()
        for voice in voices
    )
    by_id = {voice["id"]: voice for voice in voices}
    assert by_id["longxiaochun_v2"] == {
        "id": "longxiaochun_v2",
        "name": "龙小淳 (知性女)",
        "gender": "Female",
        "model": "cosyvoice-v2",
        "family": "cosyvoice",
        "supports_instruction": False,
        "dialect": None,
        "lang_primary": None,
        "origin": "system",
    }
    assert by_id["Cherry"]["family"] == "qwen3"
    assert by_id["Cherry"]["supports_instruction"] is True
    assert by_id["Jada"]["dialect"] == "shanghai"
    assert by_id["Bodega"]["lang_primary"] == "es"
    assert sessions.calls[-1] == ("session-token", None)


def test_cloud_asset_crud_uses_scoped_route_and_optimistic_versions(asset_api) -> None:
    (
        _database,
        context,
        sessions,
        client,
        _source_series_id,
        _target_series_id,
        project_id,
    ) = asset_api
    created = client.post(
        f"/projects/{project_id}/characters",
        json={
            "name": "林墨",
            "description": "主角",
            "age": "22",
        },
        headers=_headers(context.workspace_id),
    )

    assert created.status_code == 201
    assert created.json()["name"] == "林墨"
    assert created.json()["scope"] == "project"
    assert created.json()["version"] == 1
    assert "source" not in created.json()
    assert sessions.calls[-1] == ("session-token", "csrf-token")

    asset_id = created.json()["id"]
    missing_version = client.post(
        f"/projects/{project_id}/assets/toggle_starred",
        json={"asset_id": asset_id, "asset_type": "character"},
        headers=_headers(context.workspace_id),
    )
    assert missing_version.status_code == 428
    assert missing_version.json()["code"] == "ASSET_VERSION_REQUIRED"

    updated = client.post(
        f"/projects/{project_id}/assets/toggle_starred",
        json={"asset_id": asset_id, "asset_type": "character"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert updated.status_code == 200
    assert updated.json()["starred"] is True
    assert updated.json()["version"] == 2

    stale = client.post(
        f"/projects/{project_id}/assets/toggle_lock",
        json={"asset_id": asset_id, "asset_type": "character"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONTENT_VERSION_CONFLICT"

    deleted = client.delete(
        f"/projects/{project_id}/characters/{asset_id}",
        headers=_headers(context.workspace_id, version=2),
    )
    assert deleted.status_code == 200
    listing = client.get(
        f"/projects/{project_id}/assets",
        headers=_headers(context.workspace_id),
    )
    assert listing.json()["characters"] == []


def test_cloud_asset_routes_hide_other_workspace_resources(asset_api) -> None:
    (
        database,
        context,
        _sessions,
        client,
        _source_series_id,
        _target_series_id,
        project_id,
    ) = asset_api
    with database.session_factory.begin() as session:
        workspace = WorkspaceRecord(
            user_id=int(context.identity.user_id),
            name="其他工作区",
        )
        session.add(workspace)
        session.flush()
        other_workspace_id = workspace.id

    response = client.get(
        f"/projects/{project_id}/assets",
        headers=_headers(str(other_workspace_id)),
    )
    assert response.status_code == 404
    assert response.json() == {
        "code": "CONTENT_NOT_FOUND",
        "message": "资源不存在",
    }


def test_generated_asset_media_becomes_selected_variant(asset_api) -> None:
    (
        database,
        context,
        _sessions,
        client,
        _source_series_id,
        _target_series_id,
        project_id,
    ) = asset_api
    character = client.post(
        f"/projects/{project_id}/characters",
        json={"name": "林墨", "description": "主角"},
        headers=_headers(context.workspace_id),
    ).json()
    media = CloudMediaStorage(database, FakePrivateObjectStore()).store(
        context,
        MediaWrite(
            content=b"generated-image",
            content_type="image/png",
            filename="lin-mo.png",
            project_id=project_id,
            provenance={"origin": "ai_task"},
        ),
    )

    first = client.post(
        f"/projects/{project_id}/assets/update_image",
        json={
            "asset_id": character["id"],
            "asset_type": "character",
            "media_id": media.media_id,
        },
        headers=_headers(context.workspace_id, version=character["version"]),
    )

    assert first.status_code == 200, first.text
    reference = f"media:{media.media_id}"
    payload = first.json()
    assert payload["image_url"] == reference
    assert payload["avatar_url"] == reference
    assert payload["reference_sheet"]["selected_image_id"]
    assert [
        item["url"] for item in payload["reference_sheet"]["image_variants"]
    ] == [reference]

    repeated = client.post(
        f"/projects/{project_id}/assets/update_image",
        json={
            "asset_id": character["id"],
            "asset_type": "character",
            "media_id": media.media_id,
        },
        headers=_headers(context.workspace_id, version=payload["version"]),
    )
    assert repeated.status_code == 200, repeated.text
    assert len(repeated.json()["reference_sheet"]["image_variants"]) == 1


def test_library_promotion_fork_and_series_import_are_scoped(asset_api) -> None:
    (
        _database,
        context,
        _sessions,
        client,
        source_series_id,
        target_series_id,
        project_id,
    ) = asset_api
    project_asset = client.post(
        f"/projects/{project_id}/props",
        json={"name": "怀表", "description": "项目道具"},
        headers=_headers(context.workspace_id),
    ).json()
    promoted = client.post(
        "/library/assets/promote",
        json={
            "source_kind": "project",
            "source_id": project_id,
            "asset_type": "prop",
            "asset_id": project_asset["id"],
        },
        headers=_headers(context.workspace_id),
    )
    assert promoted.status_code == 201
    assert promoted.json()["scope"] == "workspace"
    assert promoted.json()["id"] != project_asset["id"]

    forked = client.post(
        f"/projects/{project_id}/assets/fork_from_library",
        json={
            "asset_type": "prop",
            "library_asset_id": promoted.json()["id"],
        },
        headers=_headers(context.workspace_id),
    )
    assert forked.status_code == 201
    assert forked.json()["scope"] == "project"
    assert forked.json()["provenance"]["origin"] == "forked"

    source_asset = client.post(
        f"/series/{source_series_id}/scenes",
        json={"name": "旧车站", "description": "雨夜"},
        headers=_headers(context.workspace_id),
    ).json()
    imported = client.post(
        f"/series/{target_series_id}/assets/import",
        json={
            "source_series_id": source_series_id,
            "asset_ids": [source_asset["id"], "missing"],
        },
        headers=_headers(context.workspace_id),
    )
    assert imported.status_code == 200
    assert len(imported.json()["imported"]) == 1
    assert imported.json()["skipped_ids"] == ["missing"]


def test_system_library_asset_is_visible_but_read_only(asset_api) -> None:
    (
        database,
        context,
        _sessions,
        client,
        _source_series_id,
        _target_series_id,
        _project_id,
    ) = asset_api
    system_character = Character(
        id="system-hero",
        name="系统角色",
        description="平台只读角色",
    )
    with database.session_factory.begin() as session:
        session.add(
            AssetRecord(
                user_id=None,
                workspace_id=None,
                project_id=None,
                series_id=None,
                media_object_id=None,
                scope="system",
                asset_type="character",
                name=system_character.name,
                payload=system_character.model_dump(mode="json"),
                provenance={"origin": "platform"},
                schema_version=1,
                version=1,
            )
        )

    listing = client.get(
        "/library/assets",
        headers=_headers(context.workspace_id),
    )
    assert listing.status_code == 200
    assert listing.json()["characters"][0]["scope"] == "system"

    update = client.patch(
        "/library/assets/character/system-hero",
        json={"name": "尝试改名"},
        headers=_headers(context.workspace_id, version=1),
    )
    assert update.status_code == 403
    assert update.json()["code"] == "ASSET_MUTATION_FORBIDDEN"


def test_custom_voice_persistence_and_ai_routes_do_not_fall_back(asset_api) -> None:
    (
        _database,
        context,
        _sessions,
        client,
        source_series_id,
        _target_series_id,
        project_id,
    ) = asset_api
    character = client.post(
        f"/projects/{project_id}/characters",
        json={"name": "林墨", "description": "主角"},
        headers=_headers(context.workspace_id),
    ).json()
    voice = client.post(
        "/voice/design/accept",
        json={
            "series_id": source_series_id,
            "voice_id": "voice-owned",
            "voice_prompt": "冷静的青年男声",
            "label": "林墨音色",
        },
        headers=_headers(context.workspace_id),
    )
    assert voice.status_code == 201
    assert voice.json()["scope"] == "series"

    bound = client.post(
        f"/projects/{project_id}/characters/{character['id']}/voice",
        json={"voice_id": "voice-owned", "voice_name": "林墨音色"},
        headers=_headers(context.workspace_id, version=character["version"]),
    )
    assert bound.status_code == 200
    assert bound.json()["voice_id"] == "voice-owned"

    tuned = client.put(
        f"/projects/{project_id}/characters/{character['id']}/voice_params",
        json={"speed": 1.1, "pitch": 0.9, "volume": 60},
        headers=_headers(context.workspace_id, version=bound.json()["version"]),
    )
    assert tuned.status_code == 200
    assert tuned.json()["voice_volume"] == 60

    blocked = client.post(
        "/voice/preview",
        json={"voice_id": "voice-owned", "text": "测试"},
        headers=_headers(context.workspace_id),
    )
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "AI_GATEWAY_NOT_READY"

    override = client.post(
        "/voice/preview",
        json={
            "voice_id": "voice-owned",
            "text": "测试",
            "price": 0,
        },
        headers=_headers(context.workspace_id),
    )
    assert override.status_code == 422
    assert override.json()["detail"]["code"] == "AI_OVERRIDE_FORBIDDEN"

    model_override = client.post(
        "/voice/design/accept",
        json={
            "series_id": source_series_id,
            "voice_id": "voice-other",
            "voice_prompt": "青年男声",
            "label": "其他音色",
            "target_model": "client-selected-model",
        },
        headers=_headers(context.workspace_id),
    )
    assert model_override.status_code == 422
