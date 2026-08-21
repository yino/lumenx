from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.apps.comic_gen.llm_adapter import LLMAdapter
from src.models.base import VideoGenModel
from src.models.image import ImageGenModel, WanxImageModel
from src.models.mulerouter import _submit_task
from src.models.wanx import WanxModel


class FakeImageModel(ImageGenModel):
    def generate(self, prompt, output_path, **kwargs):
        return output_path, 1.5


class FakeVideoModel(VideoGenModel):
    def generate(self, prompt, output_path, **kwargs):
        return output_path, 2.5


class FakeResponse:
    def __init__(self, payload, *, headers=None, text="response") -> None:
        self.payload = payload
        self.headers = headers or {}
        self.text = text
        self.status_code = 200

    def json(self):
        return self.payload


def test_generation_adapters_return_normalized_usage_metadata() -> None:
    image = FakeImageModel({}).generate_with_usage(
        "画面",
        "/tmp/image.png",
        n=2,
        size="1024x1024",
    )
    video = FakeVideoModel({}).generate_with_usage(
        "镜头",
        "/tmp/video.mp4",
        duration=8,
        resolution="1080p",
        generate_audio=True,
    )

    assert image.raw_usage == {
        "output_count": 2,
        "resolution": "1024x1024",
    }
    assert video.raw_usage == {
        "audio": True,
        "duration_seconds": 8,
        "output_count": 1,
        "resolution": "1080p",
    }


def test_llm_adapter_returns_provider_token_usage_and_request_id() -> None:
    captured = {}
    response = SimpleNamespace(
        _request_id="request-llm-1",
        choices=[SimpleNamespace(message=SimpleNamespace(content="分析结果"))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: captured.update(kwargs) or response,
            ),
        )
    )
    adapter = LLMAdapter(provider="openai", api_key="test-key", model="qwen-test")
    adapter._client = client

    result = adapter.chat_with_usage(
        [{"role": "user", "content": "分析剧本"}],
        max_tokens=8192,
    )

    assert result.content == "分析结果"
    assert result.raw_usage == {"input_tokens": 120, "output_tokens": 30}
    assert result.provider_request_id == "request-llm-1"
    assert captured["max_tokens"] == 8192


def test_dashscope_structured_call_can_disable_thinking() -> None:
    captured = {}
    response = SimpleNamespace(
        _request_id="request-structured-1",
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"frames": []}'))],
        usage=SimpleNamespace(prompt_tokens=80, completion_tokens=20),
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: captured.update(kwargs) or response,
            ),
        )
    )
    adapter = LLMAdapter(provider="dashscope", api_key="test-key", model="qwen-test")
    adapter._client = client

    adapter.chat_with_usage(
        [{"role": "user", "content": "生成 JSON"}],
        response_format={"type": "json_object"},
        max_tokens=8192,
        enable_thinking=False,
    )

    assert captured["extra_body"] == {"enable_thinking": False}


def test_dashscope_image_persistence_failure_stops_before_polling(monkeypatch) -> None:
    polls = []
    monkeypatch.setattr(
        "src.models.image.requests.post",
        lambda *args, **kwargs: FakeResponse(
            {
                "request_id": "request-image-1",
                "output": {"task_id": "task-image-1"},
            }
        ),
    )
    monkeypatch.setattr(
        "src.models.image.requests.get",
        lambda *args, **kwargs: polls.append(args) or None,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        WanxImageModel({"api_key": "test-key"})._generate_dashscope_image_http(
            "画面",
            "wan2.7-image",
            on_provider_ids=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("database unavailable")
            ),
        )

    assert polls == []


def test_dashscope_image_submission_and_polling_recover_from_ssl_interruptions(monkeypatch) -> None:
    submission_attempts = []
    poll_attempts = []

    def submit(*_args, **_kwargs):
        submission_attempts.append(True)
        if len(submission_attempts) == 1:
            raise __import__("requests").exceptions.SSLError("unexpected EOF")
        return FakeResponse({
            "request_id": "request-image-retry",
            "output": {"task_id": "task-image-retry"},
        })

    def poll(*_args, **_kwargs):
        poll_attempts.append(True)
        if len(poll_attempts) == 1:
            raise __import__("requests").exceptions.SSLError("connection reset")
        return FakeResponse({
            "output": {
                "task_status": "SUCCEEDED",
                "choices": [{
                    "message": {"content": [{"image": "https://example.com/result.png"}]},
                }],
            },
        })

    monkeypatch.setattr("src.models.image.requests.post", submit)
    monkeypatch.setattr("src.models.image.requests.get", poll)
    monkeypatch.setattr("src.models.image.time.sleep", lambda *_args: None)
    captured = {}

    image_url = WanxImageModel({"api_key": "test-key"})._generate_dashscope_image_http(
        "雨夜巷战",
        "wan2.7-image-pro",
        on_provider_ids=lambda provider, provider_task_id, request_id: captured.update(
            provider=provider,
            task_id=provider_task_id,
            request_id=request_id,
        ),
    )

    assert image_url == "https://example.com/result.png"
    assert len(submission_attempts) == 2
    assert len(poll_attempts) == 2
    assert captured == {
        "provider": "dashscope",
        "task_id": "task-image-retry",
        "request_id": "request-image-retry",
    }


def test_dashscope_video_persistence_failure_stops_before_polling(monkeypatch) -> None:
    polls = []
    monkeypatch.setattr(
        "src.models.wanx.requests.post",
        lambda *args, **kwargs: FakeResponse(
            {
                "request_id": "request-video-1",
                "output": {"task_id": "task-video-1"},
            }
        ),
    )
    monkeypatch.setattr(
        "src.models.wanx.requests.get",
        lambda *args, **kwargs: polls.append(args) or None,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        WanxModel({"api_key": "test-key"})._generate_hh_http(
            prompt="镜头",
            model_name="happyhorse-1.1-t2v",
            on_provider_ids=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("database unavailable")
            ),
        )

    assert polls == []


def test_dashscope_video_submission_and_polling_recover_from_ssl_interruptions(monkeypatch) -> None:
    submission_attempts = []
    poll_attempts = []

    def submit(*_args, **_kwargs):
        submission_attempts.append(True)
        if len(submission_attempts) == 1:
            raise __import__("requests").exceptions.SSLError("unexpected EOF")
        return FakeResponse({
            "request_id": "request-video-retry",
            "output": {"task_id": "task-video-retry"},
        })

    def poll(*_args, **_kwargs):
        poll_attempts.append(True)
        if len(poll_attempts) == 1:
            raise __import__("requests").exceptions.SSLError("connection reset")
        return FakeResponse({
            "output": {
                "task_status": "SUCCEEDED",
                "video_url": "https://example.com/result.mp4",
            },
        })

    monkeypatch.setattr("src.models.wanx.requests.post", submit)
    monkeypatch.setattr("src.models.wanx.requests.get", poll)
    monkeypatch.setattr("src.models.wanx.time.sleep", lambda *_args: None)
    captured = {}

    video_url = WanxModel({"api_key": "test-key"})._generate_wan_r2v_http(
        "雨夜巷战",
        ["https://example.com/reference.png"],
        model_name="wan2.7-r2v",
        on_provider_ids=lambda provider, provider_task_id, request_id: captured.update(
            provider=provider,
            task_id=provider_task_id,
            request_id=request_id,
        ),
    )

    assert video_url == "https://example.com/result.mp4"
    assert len(submission_attempts) == 2
    assert len(poll_attempts) == 2
    assert captured == {
        "provider": "dashscope",
        "task_id": "task-video-retry",
        "request_id": "request-video-retry",
    }


def test_dashscope_video_resume_queries_existing_task_without_resubmitting(monkeypatch, tmp_path) -> None:
    submissions = []
    polls = []
    monkeypatch.setattr(
        "src.models.wanx.requests.post",
        lambda *_args, **_kwargs: submissions.append(True),
    )
    monkeypatch.setattr(
        "src.models.wanx.requests.get",
        lambda *_args, **_kwargs: polls.append(True) or FakeResponse({
            "output": {
                "task_status": "SUCCEEDED",
                "video_url": "https://example.com/recovered.mp4",
            },
        }),
    )
    monkeypatch.setattr(
        "src.models.wanx.WanxModel._download_video",
        lambda _self, _url, path: __import__("pathlib").Path(path).write_bytes(b"video"),
    )

    output_path = tmp_path / "recovered.mp4"
    path, _elapsed = WanxModel({"api_key": "test-key"}).resume_dashscope_video_task(
        provider_task_id="existing-task-1",
        output_path=str(output_path),
        model_name="wan2.7-r2v",
    )

    assert path == str(output_path)
    assert output_path.read_bytes() == b"video"
    assert submissions == []
    assert polls == [True]


def test_mulerouter_submission_callback_receives_ids(monkeypatch) -> None:
    response = FakeResponse(
        {"task_info": {"id": "task-mule-1"}},
        headers={"x-request-id": "request-mule-1"},
    )
    monkeypatch.setattr(
        "src.models.mulerouter._request_with_retry",
        lambda *args, **kwargs: response,
    )
    captured = {}

    task_id = _submit_task(
        "https://example.com",
        "/generate",
        {"prompt": "镜头"},
        api_key="test-key",
        on_provider_ids=lambda provider, provider_task_id, request_id: captured.update(
            provider=provider,
            task_id=provider_task_id,
            request_id=request_id,
        ),
    )

    assert task_id == "task-mule-1"
    assert captured == {
        "provider": "mulerouter",
        "task_id": "task-mule-1",
        "request_id": "request-mule-1",
    }
