from __future__ import annotations

import base64
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
                    "url": "https://api.xlinks.site/v1/videos/task-42/content",
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
    download_calls = [call for call in session.calls if call[0] == "GET" and call[2].get("stream")]
    assert download_calls[0][2]["headers"]["Authorization"] == "Bearer xlinks-secret"
    assert session.calls[0][2]["json"] == {
        "model": "grok-imagine-video",
        "prompt": "一个人在城市街道上慢慢走过",
        "image_url": "https://assets.example/image.png",
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


def test_xlinks_accepts_gemini_omni_model_id(tmp_path):
    provider = XlinksGrokVideoProvider(
        "gemini-omni-1.1-flash",
        SecretStr("test"),
        base_url="https://api.xlinks.site/v1",
    )
    payload = provider._request_body(
        VideoGenerationRequest(
            model_id="gemini-omni-1.1-flash",
            prompt="一个人在城市街道上慢慢走过",
            output_path=str(tmp_path / "result.mp4"),
            mode="t2v",
            parameters={"duration": 5, "resolution": "720p"},
        )
    )
    assert payload["model"] == "gemini-omni-1.1-flash"
    assert payload["duration"] == 5
    assert payload["width"] == 1280
    assert payload["height"] == 720


def test_xlinks_accepts_xlinks_wrapped_status_and_result_url(tmp_path):
    provider = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("test"),
        base_url="https://api.xlinks.site/v1",
    )

    queued_status, queued_url, _ = provider._status_payload(
        {
            "code": "success",
            "data": {
                "task_id": "task-1",
                "status": "QUEUED",
                "data": {"status": "pending", "progress": 1},
            },
        }
    )
    assert queued_status == "queued"
    assert queued_url is None

    not_started_status, _, _ = provider._status_payload(
        {"data": {"status": "not_start"}}
    )
    assert not_started_status == "queued"

    completed_status, completed_url, _ = provider._status_payload(
        {
            "code": "success",
            "data": {
                "task_id": "task-1",
                "status": "SUCCESS",
                "result_url": "https://api.xlinks.site/v1/videos/task-1/content",
                "data": {
                    "status": "done",
                    "video": {"url": "/v1/videos/task-1/content"},
                },
            },
        }
    )
    assert completed_status == "completed"
    assert completed_url == "https://api.xlinks.site/v1/videos/task-1/content"


def test_xlinks_i2v_accepts_a_base64_data_uri(tmp_path):
    image = base64.b64encode(b"png-bytes").decode("ascii")
    provider = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("test"),
        base_url="https://api.xlinks.site/v1",
    )

    payload = provider._request_body(
        _request(
            tmp_path / "result.mp4",
            inputs=(f"data:image/png;base64,{image}",),
        )
    )

    assert payload["image_url"] == f"data:image/png;base64,{image}"


def test_xlinks_r2v_sends_images_array_for_multiple_inputs(tmp_path):
    provider = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("test"),
        base_url="https://api.xlinks.site/v1",
    )

    payload = provider._request_body(
        _request(
            tmp_path / "result.mp4",
            mode="r2v",
            inputs=(
                "https://assets.example/first.png",
                "https://assets.example/second.png",
            ),
        )
    )

    assert "image_url" not in payload
    assert payload["images"] == [
        "https://assets.example/first.png",
        "https://assets.example/second.png",
    ]


def test_xlinks_r2v_images_override_supports_multiple_inline_images(tmp_path):
    provider = XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("test"),
        base_url="https://api.xlinks.site/v1",
    )

    payload = provider._request_body(
        _request(tmp_path / "result.mp4", mode="r2v"),
        images_override=(
            "data:image/png;base64,aW1n",
            "https://assets.example/remote.png",
        ),
    )

    assert payload["images"] == [
        "data:image/png;base64,aW1n",
        "https://assets.example/remote.png",
    ]


def test_xlinks_retries_fetch_rejection_with_inline_image(tmp_path, monkeypatch):
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video"
    source_image = b"source-image"

    class RetrySession(FakeSession):
        def __init__(self):
            super().__init__(
                None,
                [
                    FakeResponse(
                        422,
                        {
                            "code": "fail_to_fetch_task",
                            "message": "xAI upstream returned status 422",
                        },
                    ),
                    FakeResponse(202, {"task_id": "task-inline", "status": "queued"}),
                ],
                FakeResponse(
                    200,
                    headers={"Content-Length": str(len(mp4))},
                    content=mp4,
                ),
            )
            self.post_responses = list(self.poll_responses)
            self.poll_responses = [
                FakeResponse(200, {"task_id": "task-inline", "status": "completed", "url": "https://cdn.example/video.mp4"}),
            ]

        def post(self, url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            return self.post_responses.pop(0)

        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if kwargs.get("stream") and url == "https://assets.example/image.png":
                return FakeResponse(
                    200,
                    headers={
                        "Content-Type": "image/png",
                        "Content-Length": str(len(source_image)),
                    },
                    content=source_image,
                )
            if kwargs.get("stream"):
                return self.download_response
            return self.poll_responses.pop(0)

    monkeypatch.setattr("src.platform.video_providers.xlinks.time.sleep", lambda _: None)
    session = RetrySession()
    output = tmp_path / "result.mp4"
    XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("xlinks-secret"),
        session=session,
        base_url="https://api.xlinks.site/v1",
        poll_interval=0.01,
    ).generate(_request(output, inputs=("https://assets.example/image.png",)))

    assert output.read_bytes() == mp4
    post_calls = [call for call in session.calls if call[0] == "POST"]
    assert len(post_calls) == 2
    retry_body = post_calls[1][2]["json"]
    assert retry_body["image_url"] == (
        "data:image/png;base64," + base64.b64encode(source_image).decode("ascii")
    )
    download_calls = [
        call
        for call in session.calls
        if call[0] == "GET" and call[1] == "https://cdn.example/video.mp4"
    ]
    assert "Authorization" not in download_calls[0][2]["headers"]


def test_xlinks_retries_r2v_fetch_rejection_with_inline_images(
    tmp_path, monkeypatch
):
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00video"
    first_image = b"first-source-image"
    second_image = b"second-source-image"

    class MultiImageRetrySession(FakeSession):
        def __init__(self):
            super().__init__(
                None,
                [
                    FakeResponse(
                        422,
                        {
                            "code": "fail_to_fetch_task",
                            "message": "xAI upstream returned status 422",
                        },
                    ),
                    FakeResponse(202, {"task_id": "task-inline", "status": "queued"}),
                ],
                FakeResponse(
                    200,
                    headers={"Content-Length": str(len(mp4))},
                    content=mp4,
                ),
            )
            self.post_responses = list(self.poll_responses)
            self.poll_responses = [
                FakeResponse(
                    200,
                    {
                        "task_id": "task-inline",
                        "status": "completed",
                        "url": "https://cdn.example/video.mp4",
                    },
                ),
            ]

        def post(self, url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            return self.post_responses.pop(0)

        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if kwargs.get("stream"):
                images = {
                    "https://assets.example/first.png": first_image,
                    "https://assets.example/second.png": second_image,
                }
                if url in images:
                    return FakeResponse(
                        200,
                        headers={
                            "Content-Type": "image/png",
                            "Content-Length": str(len(images[url])),
                        },
                        content=images[url],
                    )
                return self.download_response
            return self.poll_responses.pop(0)

    monkeypatch.setattr("src.platform.video_providers.xlinks.time.sleep", lambda _: None)
    session = MultiImageRetrySession()
    output = tmp_path / "result.mp4"
    XlinksGrokVideoProvider(
        "grok-imagine-video",
        SecretStr("xlinks-secret"),
        session=session,
        base_url="https://api.xlinks.site/v1",
        poll_interval=0.01,
    ).generate(
        _request(
            output,
            mode="r2v",
            inputs=(
                "https://assets.example/first.png",
                "https://assets.example/second.png",
            ),
        )
    )

    assert output.read_bytes() == mp4
    post_calls = [call for call in session.calls if call[0] == "POST"]
    assert len(post_calls) == 2
    retry_body = post_calls[1][2]["json"]
    assert "image_url" not in retry_body
    assert retry_body["images"] == [
        "data:image/png;base64," + base64.b64encode(first_image).decode("ascii"),
        "data:image/png;base64," + base64.b64encode(second_image).decode("ascii"),
    ]


def test_xlinks_does_not_retry_non_fetch_rejections(tmp_path):
    session = FakeSession(
        FakeResponse(
            422,
            {"code": "invalid_parameter", "message": "duration is invalid"},
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

    with pytest.raises(ProviderRequestRejectedError):
        provider.generate(
            _request(
                tmp_path / "result.mp4",
                inputs=("https://assets.example/image.png",),
            )
        )

    assert len([call for call in session.calls if call[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "mode,inputs",
    [
        ("i2v", ()),
        ("i2v", ("https://a", "https://b")),
        ("r2v", ()),
        ("t2v", ("https://a",)),
    ],
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
