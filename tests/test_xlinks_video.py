from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from src.models.provider_result import ProviderGenerationResult
from src.platform.provider_errors import ProviderRequestRejectedError
from src.platform.video_providers.interface import VideoGenerationRequest
from src.platform.video_providers.xlinks import XlinksGrokVideoProvider


class FakeResponse:
    def __init__(self, status_code, payload=None, *, headers=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._content = content

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size=0):
        yield self._content


class FakeSession:
    def __init__(self, post_response, poll_responses, download_response):
        self.post_response = post_response
        self.poll_responses = list(poll_responses)
        self.download_response = download_response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if kwargs.get("stream"):
            return self.download_response
        return self.poll_responses.pop(0)


def _request(output_path: Path, *, mode="i2v", parameters=None, inputs=()):
    return VideoGenerationRequest(
        model_id="grok-imagine-video",
        prompt="一个人在城市街道上慢慢走过",
        output_path=str(output_path),
        mode=mode,
        input_urls=tuple(inputs),
        parameters=parameters or {},
    )


def test_xlinks_video_creates_polls_downloads_and_persists_task_id(
    tmp_path, monkeypatch
):
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video"
    session = FakeSession(
        FakeResponse(200, {"task_id": "task-42", "status": "queued"}, headers={"x-request-id": "req-42"}),
        [
            FakeResponse(200, {"task_id": "task-42", "status": "queued"}),
            FakeResponse(
                200,
                {
                    "task_id": "task-42",
                    "status": "completed",
                    "url": "https://cdn.example/video.mp4",
                    "metadata": {"duration": 5, "width": 1280, "height": 720},
                },
            ),
        ],
        FakeResponse(200, headers={"Content-Length": str(len(mp4))}, content=mp4),
    )
    monkeypatch.setattr("src.platform.video_providers.xlinks.time.sleep", lambda _: None)
    submitted = []
    output = tmp_path / "result.mp4"
    request = _request(
        output,
        parameters={"duration": 5, "resolution": "720p", "fps": 30},
        inputs=("https://assets.example/image.png",),
    )
    result = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("xlinks-secret"),
        session=session,
        base_url="https://api.xlinks.site/v1",
        poll_interval=0.01,
    ).generate(
        VideoGenerationRequest(
            model_id=request.model_id,
            prompt=request.prompt,
            output_path=request.output_path,
            mode=request.mode,
            input_urls=request.input_urls,
            parameters=request.parameters,
            on_provider_submission=lambda *args: submitted.append(args),
        )
    )

    assert isinstance(result, ProviderGenerationResult)
    assert output.read_bytes() == mp4
    assert submitted == [("xlinks", "task-42", "req-42")]
    assert session.calls[0][0:2] == ("POST", "https://api.xlinks.site/v1/video/generations")
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer xlinks-secret"
    assert session.calls[0][2]["json"] == {
        "model": "grok-imagine-video",
        "prompt": "一个人在城市街道上慢慢走过",
        "image": "https://assets.example/image.png",
        "duration": 5,
        "fps": 30,
        "width": 1280,
        "height": 720,
    }
    assert result.raw_usage["resolution"] == "720p"


def test_xlinks_t2v_uses_newapi_json_fields(tmp_path):
    provider = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("test"),
        base_url="https://api.xlinks.site/v1",
    )
    payload = provider._request_body(
        _request(
            tmp_path / "result.mp4",
            mode="t2v",
            parameters={
                "duration": 6,
                "resolution": "1080p",
                "seed": 0,
                "negative_prompt": "文字水印",
                "quality_level": "high",
            },
        )
    )
    assert payload == {
        "model": "grok-imagine-video",
        "prompt": "一个人在城市街道上慢慢走过",
        "duration": 6,
        "seed": 0,
        "width": 1920,
        "height": 1080,
        "metadata": {"negative_prompt": "文字水印", "quality_level": "high"},
    }


@pytest.mark.parametrize(
    "mode,inputs",
    [("i2v", ()), ("i2v", ("https://a", "https://b")), ("t2v", ("https://a",))],
)
def test_xlinks_video_rejects_invalid_input_modes(tmp_path, mode, inputs):
    provider = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("test"),
        base_url="https://api.xlinks.site/v1",
    )
    with pytest.raises(ValueError):
        provider._request_body(_request(tmp_path / "result.mp4", mode=mode, inputs=inputs))


def test_xlinks_video_redacts_authentication_rejection(tmp_path):
    session = FakeSession(
        FakeResponse(
            401,
            {"error": {"code": "invalid_api_key", "message": "bad xlinks-secret"}},
        ),
        [],
        FakeResponse(500),
    )
    provider = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("xlinks-secret"),
        session=session,
        base_url="https://api.xlinks.site/v1",
    )
    with pytest.raises(ProviderRequestRejectedError) as caught:
        provider.generate(_request(tmp_path / "result.mp4", mode="t2v"))
    assert "xlinks-secret" not in str(caught.value)
