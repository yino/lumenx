from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.apps.comic_gen.models import StoryboardFrame
from src.platform.ai_gateway import SubmittedAITask
from src.platform.ai_gateway_api import (
    effective_engine_response,
    install_cloud_ai_gateway_api,
)
from src.platform.contracts import ModelRouteSnapshot
from src.platform.asset_api import install_cloud_asset_api
from src.platform.asset_service import CloudAssetService
from src.platform.content_api import install_cloud_content_api
from src.platform.content_repositories import PostgresProjectRepository
from src.platform.playground_api import install_cloud_playground_api
from src.platform.storyboard_api import install_cloud_storyboard_api
from tests.test_content_api import FakeSessions, _headers
from tests.test_content_repositories import RepositoryDatabase, _create_scope, _script
from tests.test_content_service import FakeScriptProcessor


class RecordingSubmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def submit(self, context, payload: Mapping[str, Any]) -> SubmittedAITask:
        copied = dict(payload)
        self.calls.append((context, copied))
        return SubmittedAITask(
            task_id="101",
            attempt_id="201",
            status="queued",
            capability=str(copied["capability"]),
            quoted_microtickets=250_000,
            tokens_per_ticket=1000,
            model_display_name="平台创作模型",
            provider_model_id="server-model-v1",
            reused=False,
            dispatched=True,
        )


def test_effective_image_engine_projection_exposes_only_safe_identity() -> None:
    route = ModelRouteSnapshot(
        config_version_id="config-secret",
        route_id="route-secret",
        capability="image.t2i",
        provider="xlinks",
        provider_model_id="gpt-image-2",
        display_name="GPT Image 2",
        parameters={"size": "1024x1024"},
        metering_formula={"kind": "image", "per_image_tokens": 99},
        fallback_policy={"enabled": False},
        secret_ref="XLINKS_API_KEY",
    )

    projection = effective_engine_response(route)

    assert projection == {
        "capability": "image.t2i",
        "model_id": "gpt-image-2",
        "model_display_name": "GPT Image 2",
        "provider_display_name": "Xlinks",
        "features": {"character_design_sheet": True},
    }
    serialized = str(projection)
    assert "XLINKS_API_KEY" not in serialized
    assert "route-secret" not in serialized
    assert "config-secret" not in serialized


@pytest.fixture
def cloud_ai_routes():
    database = RepositoryDatabase()
    context = _create_scope(database)
    principal = SimpleNamespace(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
    )
    sessions = FakeSessions(principal)
    auth = SimpleNamespace(database=database, sessions=sessions)
    submitter = RecordingSubmitter()
    app = FastAPI()
    install_cloud_ai_gateway_api(app, auth, submitter=submitter)
    install_cloud_content_api(
        app,
        auth,
        script_processor=FakeScriptProcessor(),
        ai_submitter=submitter,
    )
    install_cloud_asset_api(app, auth, ai_submitter=submitter)
    placeholder_media = SimpleNamespace()
    install_cloud_storyboard_api(
        app,
        auth,
        placeholder_media,
        ai_submitter=submitter,
    )
    install_cloud_playground_api(
        app,
        auth,
        placeholder_media,
        ai_submitter=submitter,
    )
    client = TestClient(app)
    client.cookies.set("lumenx_session", "session-token")
    yield database, context, sessions, submitter, client
    client.close()
    database.engine.dispose()


def _ai_headers(workspace_id: str, key: str) -> dict[str, str]:
    return {**_headers(workspace_id), "Idempotency-Key": key}


def test_project_creation_and_reparse_submit_script_tasks(cloud_ai_routes) -> None:
    _database, context, _sessions, submitter, client = cloud_ai_routes
    created = client.post(
        "/projects",
        json={"title": "云端项目", "text": "故事正文", "workflow_mode": "r2v"},
        headers=_ai_headers(context.workspace_id, "project-create-1"),
    )

    assert created.status_code == 201
    assert created.json()["ai_task"]["capability"] == "script.analysis"
    project_id = created.json()["id"]
    assert submitter.calls[-1][1]["project_id"] == project_id
    assert submitter.calls[-1][1]["content"]["operation"] == (
        "project.create_and_analyze"
    )

    reparsed = client.put(
        f"/projects/{project_id}/reparse",
        json={"text": "更新后的故事"},
        headers={
            **_ai_headers(context.workspace_id, "project-reparse-1"),
            "If-Match": str(created.json()["version"]),
        },
    )
    assert reparsed.status_code == 200
    assert reparsed.json()["ai_task"]["capability"] == "script.analysis"
    assert submitter.calls[-1][1]["content"]["operation"] == "project.reparse"


def test_dialogue_audio_batch_builds_server_owned_speech_items(cloud_ai_routes) -> None:
    database, context, _sessions, submitter, client = cloud_ai_routes
    project = PostgresProjectRepository(database).add(
        context,
        _script(
            frames=[
                StoryboardFrame(
                    id="frame-dialogue-1",
                    scene_id="scene-1",
                    dialogue="你终于来了。",
                    speaker="张成",
                )
            ]
        ),
    )
    character = CloudAssetService(database).create_project_asset(
        context,
        project.document.id,
        "character",
        name="张成",
        description="疲惫的青年",
        voice_id="longcheng_v2",
    )

    response = client.post(
        f"/projects/{project.document.id}/dialogue_audio/batch",
        headers=_ai_headers(context.workspace_id, "dialogue-batch-1"),
    )

    assert response.status_code == 202, response.text
    assert response.json()["capability"] == "speech.tts"
    submitted = submitter.calls[-1][1]
    assert submitted["project_id"] == project.document.id
    assert submitted["content"]["operation"] == "audio.dialogue.batch"
    assert submitted["content"]["items"] == [
        {
            "frame_id": "frame-dialogue-1",
            "text": "你终于来了。",
            "voice_id": "longcheng_v2",
            "speed": 1.0,
            "pitch": 1.0,
            "volume": 50,
            "instructions": None,
            "dialogue_text_hash": submitted["content"]["items"][0][
                "dialogue_text_hash"
            ],
        }
    ]
    assert character.domain_id == character.document.id


def test_asset_storyboard_video_voice_audio_and_playground_use_gateway(
    cloud_ai_routes,
) -> None:
    _database, context, _sessions, submitter, client = cloud_ai_routes
    project_id = "11"
    frame_id = "12"
    media_id = "13"
    asset_id = "14"
    requests = [
        (
            f"/projects/{project_id}/extract_preview",
            {"content": {"text": "提取角色、场景和道具"}},
            "extract-preview-1",
            "script.analysis",
        ),
        (
            f"/projects/{project_id}/assets/generate",
            {"content": {"asset_type": "character", "prompt": "少年剑客"}},
            "asset-generate-1",
            "image.t2i",
        ),
        (
            f"/projects/{project_id}/assets/character/{asset_id}/generate_video",
            {
                "content": {"prompt": "角色转身看向镜头"},
                "media_ids": [media_id],
            },
            "asset-video-1",
            "video.i2v",
        ),
        (
            f"/projects/{project_id}/storyboard/refine_prompt",
            {"content": "润色分镜提示词"},
            "storyboard-polish-1",
            "prompt.polish",
        ),
        (
            f"/projects/{project_id}/generate_video",
            {
                "content": {"prompt": "镜头缓慢推进"},
                "media_ids": [media_id],
                "parameters": {"duration_seconds": 5},
            },
            "video-generate-1",
            "video.i2v",
        ),
        (
            f"/projects/{project_id}/frames/{frame_id}/audio",
            {"content": {"text": "你终于来了"}},
            "speech-generate-1",
            "speech.tts",
        ),
        (
            f"/projects/{project_id}/mix/generate_sfx",
            {"content": {"description": "雨夜雷声"}},
            "sfx-generate-1",
            "audio.sfx",
        ),
        (
            "/voice/design/translate",
            {"content": "冷静而克制的青年男声"},
            "voice-translate-1",
            "prompt.polish",
        ),
        (
            "/playground/generate",
            {
                "mode": "v2v",
                "content": {"prompt": "改成雨夜氛围"},
                "media_ids": [media_id],
            },
            "playground-v2v-1",
            "video.v2v",
        ),
    ]

    for path, payload, key, capability in requests:
        response = client.post(
            path,
            json=payload,
            headers=_ai_headers(context.workspace_id, key),
        )
        assert response.status_code == 202, response.text
        assert response.json()["capability"] == capability
        assert response.json()["actual_model"] == {
            "display_name": "平台创作模型",
            "model_id": "server-model-v1",
        }

    assert [call[1]["capability"] for call in submitter.calls] == [
        item[3] for item in requests
    ]
    video_call = submitter.calls[4][1]
    assert video_call["media_ids"] == [media_id]
    assert video_call["parameters"] == {"duration_seconds": 5}
    speech_call = submitter.calls[5][1]
    assert speech_call["resource_ids"] == {"storyboard_frame": [frame_id]}


def test_r2v_video_task_uses_reference_video_capability(cloud_ai_routes) -> None:
    _database, context, _sessions, submitter, client = cloud_ai_routes

    response = client.post(
        "/projects/11/video_tasks",
        json={
            "prompt": "角色沿长廊向镜头走来",
            "generation_mode": "r2v",
            "media_ids": ["13", "14"],
            "parameters": {"duration": 5, "resolution": "720p"},
        },
        headers=_ai_headers(context.workspace_id, "video-r2v-1"),
    )

    assert response.status_code == 202, response.text
    assert response.json()["capability"] == "video.r2v"
    assert submitter.calls[-1][1]["capability"] == "video.r2v"
    assert submitter.calls[-1][1]["media_ids"] == ["13", "14"]


def test_video_prompt_polish_associates_body_script_id(cloud_ai_routes) -> None:
    _database, context, _sessions, submitter, client = cloud_ai_routes

    response = client.post(
        "/video/polish_r2v_prompt",
        json={
            "draft_prompt": "[character1:张成]望向酒店窗口",
            "slots": [{"description": "张成：疲惫的普通男性"}],
            "script_id": "5",
            "media_ids": ["13"],
        },
        headers=_ai_headers(context.workspace_id, "r2v-polish-1"),
    )

    assert response.status_code == 202, response.text
    submitted = submitter.calls[-1][1]
    assert submitted["project_id"] == "5"
    assert submitted["capability"] == "prompt.polish"
    assert submitted["media_ids"] == ["13"]
    assert submitted["content"]["operation"] == "video.r2v_prompt.polish"


def test_gateway_routes_require_idempotency_and_reject_client_model_control(
    cloud_ai_routes,
) -> None:
    _database, context, _sessions, submitter, client = cloud_ai_routes
    project_id = "11"
    missing_key = client.post(
        f"/projects/{project_id}/assets/generate",
        json={"content": "角色立绘"},
        headers=_headers(context.workspace_id),
    )
    override = client.post(
        "/playground/generate",
        json={"mode": "t2i", "model_id": "client-model", "content": "测试"},
        headers=_ai_headers(context.workspace_id, "override-1"),
    )
    multipart = client.post(
        "/voice/clone",
        files={"file": ("voice.wav", b"audio", "audio/wav")},
        headers=_ai_headers(context.workspace_id, "voice-clone-1"),
    )

    assert missing_key.status_code == 422
    assert missing_key.json()["code"] == "AI_IDEMPOTENCY_REQUIRED"
    assert override.status_code == 422
    assert override.json()["code"] == "AI_OVERRIDE_FORBIDDEN"
    assert multipart.status_code == 422
    assert multipart.json()["code"] == "AI_MEDIA_ID_REQUIRED"
    assert submitter.calls == []


def test_unified_ai_endpoint_passes_only_authenticated_workspace_context(
    cloud_ai_routes,
) -> None:
    _database, context, sessions, submitter, client = cloud_ai_routes
    response = client.post(
        "/ai/generate",
        json={
            "capability": "prompt.polish",
            "idempotency_key": "unified-ai-1",
            "content": "润色这段提示词",
            "parameters": {},
        },
        headers=_headers(context.workspace_id),
    )

    assert response.status_code == 202
    assert int(response.json()["task_id"]) > 0
    assert submitter.calls[-1][1]["capability"] == "prompt.polish"
    assert submitter.calls[-1][0].workspace_id == context.workspace_id
    assert submitter.calls[-1][0].identity.user_id == context.identity.user_id
    assert sessions.calls[-1] == ("session-token", "csrf-token")


def test_next_episode_hook_uses_server_prompt_and_persistent_text_task(
    cloud_ai_routes,
) -> None:
    _database, context, _sessions, submitter, client = cloud_ai_routes
    script_text = "不应进入结尾上下文的前文" + ("雨夜追逐，门后传来脚步声。" * 120)
    project = client.post(
        "/projects?skip_analysis=true",
        json={"title": "云端钩子", "text": script_text},
        headers=_headers(context.workspace_id),
    ).json()

    response = client.post(
        f"/projects/{project['id']}/next_hook",
        json={"content": "浏览器伪造的提示词", "model_id": "client-model"},
        headers=_ai_headers(context.workspace_id, "next-hook-1"),
    )

    assert response.status_code == 202
    assert response.json()["capability"] == "prompt.polish"
    assert response.json()["version"] == project["version"]
    assert len(submitter.calls) == 1
    submitted = submitter.calls[0][1]
    assert submitted["project_id"] == project["id"]
    assert submitted["capability"] == "prompt.polish"
    assert submitted["idempotency_key"] == "next-hook-1"
    assert isinstance(submitted["content"], str)
    assert "浏览器伪造的提示词" not in submitted["content"]
    assert "client-model" not in submitted["content"]
    assert submitted["content"].endswith(script_text[-1500:])
    assert "不应进入结尾上下文的前文" not in submitted["content"]

    empty_project = client.post(
        "/projects?skip_analysis=true",
        json={"title": "空剧本", "text": ""},
        headers=_headers(context.workspace_id),
    ).json()
    empty_response = client.post(
        f"/projects/{empty_project['id']}/next_hook",
        headers=_ai_headers(context.workspace_id, "next-hook-empty"),
    )
    assert empty_response.status_code == 400
    assert empty_response.json() == {
        "code": "PROJECT_TEXT_REQUIRED",
        "message": "请先填写本集剧本文本",
    }
    assert len(submitter.calls) == 1


def test_previous_episode_summary_uses_stored_previous_text_and_text_task(
    cloud_ai_routes,
) -> None:
    _database, context, _sessions, submitter, client = cloud_ai_routes
    series = client.post(
        "/series",
        json={"title": "第一季"},
        headers=_headers(context.workspace_id),
    ).json()
    previous = client.post(
        "/projects?skip_analysis=true",
        json={
            "title": "第一集",
            "text": "上一集的真实剧本文本",
            "series_id": series["id"],
        },
        headers=_headers(context.workspace_id),
    ).json()
    current = client.post(
        "/projects?skip_analysis=true",
        json={"title": "第二集", "text": "本集正文", "series_id": series["id"]},
        headers=_headers(context.workspace_id),
    ).json()

    response = client.post(
        f"/projects/{current['id']}/previous_episode/summary",
        json={"content": "浏览器伪造摘要原文"},
        headers=_ai_headers(context.workspace_id, "previous-summary-1"),
    )

    assert response.status_code == 202
    assert response.json()["capability"] == "prompt.polish"
    assert response.json()["version"] == current["version"]
    assert response.json()["previous_episode_id"] == previous["id"]
    assert response.json()["previous_episode_title"] == "第一集"
    assert response.json()["previous_episode_version"] == previous["version"]
    assert len(submitter.calls) == 1
    submitted = submitter.calls[0][1]
    assert submitted["project_id"] == current["id"]
    assert submitted["capability"] == "prompt.polish"
    assert submitted["idempotency_key"] == "previous-summary-1"
    assert submitted["content"].endswith("上一集的真实剧本文本")
    assert "浏览器伪造摘要原文" not in submitted["content"]

    standalone = client.post(
        "/projects?skip_analysis=true",
        json={"title": "独立项目", "text": "正文"},
        headers=_headers(context.workspace_id),
    ).json()
    unavailable = client.post(
        f"/projects/{standalone['id']}/previous_episode/summary",
        headers=_ai_headers(context.workspace_id, "previous-summary-none"),
    )
    assert unavailable.status_code == 400
    assert unavailable.json() == {
        "code": "PREVIOUS_EPISODE_UNAVAILABLE",
        "message": "当前项目没有可回顾的上一集",
    }
    assert len(submitter.calls) == 1
