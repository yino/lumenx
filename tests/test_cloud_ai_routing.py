from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.ai_gateway import SubmittedAITask
from src.platform.ai_gateway_api import install_cloud_ai_gateway_api
from src.platform.asset_api import install_cloud_asset_api
from src.platform.content_api import install_cloud_content_api
from src.platform.playground_api import install_cloud_playground_api
from src.platform.storyboard_api import install_cloud_storyboard_api
from tests.test_content_api import FakeSessions, _headers
from tests.test_content_repositories import RepositoryDatabase, _create_scope
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


@pytest.fixture
def cloud_ai_routes():
    database = RepositoryDatabase()
    context = _create_scope(database)
    principal = SimpleNamespace(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
        is_platform_admin=False,
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
