from pathlib import Path
from types import SimpleNamespace

import pytest

from src.apps.comic_gen.models import VideoTask
from src.apps.comic_gen.pipeline import ComicGenPipeline
from src.models.ark_seedance import (
    AGENT_PLAN_MODEL,
    STANDARD_MODEL,
    ArkSeedanceVideoModel,
)
from src.platform.provider_errors import ProviderRequestRejectedError


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None, url=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.headers = headers or {}
        self.url = url
        self.text = str(self._payload)

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield self.content


def test_model_name_follows_agent_plan_or_standard_base_url():
    agent_plan = ArkSeedanceVideoModel(
        {
            "api_key": "test-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
        }
    )
    standard = ArkSeedanceVideoModel(
        {
            "api_key": "test-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        }
    )

    assert agent_plan.model_name == AGENT_PLAN_MODEL
    assert standard.model_name == STANDARD_MODEL


def test_builds_official_t2v_i2v_and_r2v_content_payloads():
    model = ArkSeedanceVideoModel(
        {
            "api_key": "test-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
        }
    )

    t2v = model._build_request_body(
        prompt="云海日出",
        generation_mode="t2v",
        duration=6,
        resolution="720P",
        ratio="16:9",
        seed=42,
        watermark=True,
        generate_audio=True,
    )
    assert t2v["model"] == "doubao-seedance-2.0"
    assert t2v["content"] == [{"type": "text", "text": "云海日出"}]
    assert t2v["resolution"] == "720p"
    assert t2v["generate_audio"] is True
    assert t2v["seed"] == 42

    i2v = model._build_request_body(
        prompt="人物转身",
        img_url="https://example.com/first.png",
        generation_mode="i2v",
    )
    assert i2v["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/first.png"},
    }

    r2v = model._build_request_body(
        prompt="保持角色一致",
        img_url="https://example.com/ref-1.png",
        generation_mode="r2v",
        ref_image_urls=[
            "https://example.com/ref-1.png",
            "https://example.com/ref-2.png",
        ],
    )
    image_items = r2v["content"][1:]
    assert len(image_items) == 2
    assert all(item["role"] == "reference_image" for item in image_items)


def test_normalizes_stale_duration_to_seedance_range():
    model = ArkSeedanceVideoModel(
        {
            "api_key": "test-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        }
    )

    assert model._build_request_body(prompt="demo", duration=3)["duration"] == 4
    assert model._build_request_body(prompt="demo", duration=18)["duration"] == 15
    assert model._build_request_body(prompt="demo", duration=None)["duration"] == 5


def test_submit_poll_and_download_agent_plan_video(monkeypatch, tmp_path):
    captured = {"polls": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["submit_url"] = url
        captured["headers"] = dict(headers or {})
        captured["body"] = json
        captured["submit_timeout"] = timeout
        return _FakeResponse(
            payload={"id": "cgt-test-1"},
            headers={"x-request-id": "req-test-1"},
        )

    def fake_get(url, headers=None, timeout=None, stream=False):
        del headers, timeout
        if url.endswith("/cgt-test-1"):
            captured["polls"] += 1
            if captured["polls"] == 1:
                return _FakeResponse(payload={"id": "cgt-test-1", "status": "running"})
            return _FakeResponse(
                payload={
                    "id": "cgt-test-1",
                    "status": "succeeded",
                    "content": {"video_url": "https://example.com/video.mp4"},
                }
            )
        assert stream is True
        return _FakeResponse(content=b"test-video")

    monkeypatch.setattr("src.models.ark_seedance.requests.post", fake_post)
    monkeypatch.setattr("src.models.ark_seedance.requests.get", fake_get)
    monkeypatch.setattr("src.models.ark_seedance.time.sleep", lambda _: None)

    provider_ids = {}
    model = ArkSeedanceVideoModel(
        {
            "api_key": "test-key",
            "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
            "poll_interval": 0.01,
        }
    )
    output_path = tmp_path / "result.mp4"
    result_path, _ = model.generate(
        prompt="镜头缓慢推进",
        output_path=str(output_path),
        generation_mode="t2v",
        on_provider_ids=lambda provider, task_id, request_id: provider_ids.update(
            provider=provider,
            task_id=task_id,
            request_id=request_id,
        ),
    )

    assert captured["submit_url"] == (
        "https://ark.cn-beijing.volces.com/api/plan/v3/contents/generations/tasks"
    )
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "doubao-seedance-2.0"
    assert captured["polls"] == 2
    assert provider_ids == {
        "provider": "volcengine-ark",
        "task_id": "cgt-test-1",
        "request_id": "req-test-1",
    }
    assert result_path == str(output_path)
    assert output_path.read_bytes() == b"test-video"


def test_task_failure_surfaces_official_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.models.ark_seedance.requests.post",
        lambda *args, **kwargs: _FakeResponse(payload={"id": "cgt-failed"}),
    )
    monkeypatch.setattr(
        "src.models.ark_seedance.requests.get",
        lambda *args, **kwargs: _FakeResponse(
            payload={
                "id": "cgt-failed",
                "status": "failed",
                "error": {"code": "InvalidParameter", "message": "bad content"},
            }
        ),
    )
    model = ArkSeedanceVideoModel({"api_key": "test-key", "poll_interval": 0.01})

    with pytest.raises(RuntimeError, match="InvalidParameter - bad content"):
        model.generate(
            prompt="demo",
            output_path=str(tmp_path / "failed.mp4"),
            generation_mode="t2v",
        )


def test_pipeline_routes_seedance_ark_backend(monkeypatch):
    task = VideoTask(
        id="task-seedance-ark",
        project_id="script-1",
        image_url="https://example.com/ref.png",
        prompt="demo",
        model="seedance-2.0-i2v",
        generation_mode="i2v",
        generate_audio=True,
    )
    script = SimpleNamespace(
        id="script-1",
        video_tasks=[task],
        characters=[],
        scenes=[],
        props=[],
        updated_at=0,
    )
    calls = {}

    class FakeArkModel:
        def __init__(self, config):
            calls["init"] = config

        def generate(self, **kwargs):
            calls["generate"] = kwargs
            kwargs["on_provider_ids"]("volcengine-ark", "cgt-1", "req-1")
            return kwargs["output_path"], 0.0

    monkeypatch.setattr("src.models.ark_seedance.ArkSeedanceVideoModel", FakeArkModel)

    pipeline = ComicGenPipeline.__new__(ComicGenPipeline)
    pipeline.scripts = {"script-1": script}
    pipeline._save_data = lambda: None
    pipeline._download_temp_image = lambda _: "/tmp/downloaded-seedance.png"
    pipeline._resolve_video_backend = lambda _: "ark"
    pipeline._ark_seedance_video_model = None
    pipeline._mulerouter_video_model = None
    pipeline._kling_model = None
    pipeline._vidu_model = None
    pipeline.video_generator = SimpleNamespace(
        model=SimpleNamespace(generate=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Wanx path should not be used")
        ))
    )
    pipeline.get_script = lambda script_id: pipeline.scripts.get(script_id)

    pipeline.process_video_task("script-1", "task-seedance-ark")

    assert task.status == "completed"
    assert calls["generate"]["generation_mode"] == "i2v"
    assert calls["generate"]["generate_audio"] is True
    assert calls["generate"]["img_path"] == "/tmp/downloaded-seedance.png"
    assert task.provider_name == "volcengine-ark"
    assert task.provider_task_id == "cgt-1"
    assert task.provider_request_id == "req-1"


def test_agent_plan_unsupported_model_explains_configuration():
    response = _FakeResponse(
        status_code=404,
        payload={
            "error": {
                "code": "UnsupportedModel",
                "message": "The requested model does not support the agent plan feature.",
            }
        },
        url="https://ark.cn-beijing.volces.com/api/plan/v3/contents/generations/tasks",
    )
    with pytest.raises(RuntimeError, match="未启用该 Agent Plan 视频模型"):
        ArkSeedanceVideoModel._response_json(response, action="task creation")


def test_non_video_model_error_explains_seedance_model_setting():
    response = _FakeResponse(
        status_code=400,
        payload={
            "error": {
                "code": "InvalidParameter",
                "message": "The specified model doubao-seed-2-0-lite does not support content generation.",
            }
        },
        url="https://ark.cn-beijing.volces.com/api/plan/v3/contents/generations/tasks",
    )
    with pytest.raises(RuntimeError, match="不是视频生成模型"):
        ArkSeedanceVideoModel._response_json(response, action="task creation")


def test_sensitive_input_error_is_safe_and_actionable():
    response = _FakeResponse(
        status_code=400,
        payload={
            "error": {
                "code": "InputImageSensitiveContentDetected.PrivacyInformation",
                "message": "input image may contain real person",
            }
        },
        url="https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks",
    )

    with pytest.raises(ProviderRequestRejectedError) as raised:
        ArkSeedanceVideoModel._response_json(response, action="task creation")

    assert raised.value.safe_error_code == "PROVIDER_INPUT_SENSITIVE_CONTENT"
    assert raised.value.safe_error_message == (
        "参考图片可能包含真人或隐私信息，请更换为插画/动漫图片后重试"
    )
