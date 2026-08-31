from __future__ import annotations

import base64
import json
import logging
from dataclasses import replace

import pytest
import requests
from pydantic import SecretStr

from src.models.xlinks import PNG_SIGNATURE, XlinksImageModel
from src.platform.contracts import ModelRouteSnapshot
from src.platform.model_routing import (
    ModelClientUnavailableError,
    RequestScopedModelClientFactory,
)
from src.platform.provider_errors import ProviderRequestRejectedError


PNG = PNG_SIGNATURE + b"test-png-payload"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, content=PNG):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = content

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def iter_content(self, chunk_size=64 * 1024):
        del chunk_size
        yield self.content


class FakeSession:
    def __init__(self, post_response, get_responses=None):
        self.post_response = post_response
        self.get_responses = list(get_responses or [])
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.get_responses.pop(0)


def _model(session, **config):
    return XlinksImageModel(
        {
            "api_key": "xlinks-test-secret",
            "http_session": session,
            **config,
        }
    )


def test_xlinks_posts_expected_t2i_body_and_writes_base64_png(tmp_path) -> None:
    response = FakeResponse(
        payload={"data": [{"b64_json": base64.b64encode(PNG).decode()}]},
        headers={"x-oneapi-request-id": "req-xlinks-1"},
    )
    session = FakeSession(response)
    captured = []
    output = tmp_path / "character.png"

    result = _model(session).generate_with_usage(
        "  ordinary man in a studio  ",
        str(output),
        model="gpt-image-2",
        size="720*1280",
        quality="high",
        n=1,
        output_format="png",
        background="auto",
        on_provider_ids=lambda *args: captured.append(args),
    )

    assert output.read_bytes() == PNG
    assert result.output_path == str(output)
    assert result.raw_usage == {
        "output_count": 1,
        "resolution": "1024x1536",
        "quality": "high",
        "provider_request_id": "req-xlinks-1",
    }
    assert captured == [("xlinks", None, "req-xlinks-1")]
    url, request = session.posts[0]
    assert url == "https://api.xlinks.site/v1/images/generations"
    assert request["headers"]["Authorization"] == "Bearer xlinks-test-secret"
    assert request["json"] == {
        "model": "gpt-image-2",
        "prompt": "ordinary man in a studio",
        "n": 1,
        "size": "1024x1536",
        "quality": "high",
        "output_format": "png",
        "background": "auto",
    }


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("1280x1280", "1024x1024"),
        ("1536x864", "1536x1024"),
        ("864x1536", "1024x1536"),
        ("16:9", "1536x1024"),
        ("3:4", "1024x1536"),
    ],
)
def test_xlinks_normalizes_size_by_orientation(requested, expected) -> None:
    assert XlinksImageModel._normalize_size(requested) == expected


def test_xlinks_downloads_one_https_png_with_bounded_redirects(tmp_path) -> None:
    post = FakeResponse(payload={"data": [{"url": "https://cdn.example/first"}]})
    redirect = FakeResponse(
        status_code=302,
        payload=None,
        headers={"Location": "https://cdn.example/final"},
    )
    image = FakeResponse(headers={"Content-Type": "image/png"})
    session = FakeSession(post, [redirect, image])
    output = tmp_path / "url-output.png"

    _model(session).generate("city", str(output), n=1)

    assert output.read_bytes() == PNG
    assert [item[0] for item in session.gets] == [
        "https://cdn.example/first",
        "https://cdn.example/final",
    ]
    assert all(call[1]["allow_redirects"] is False for call in session.gets)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n": 2},
        {"output_format": "jpeg"},
        {"quality": "ultra"},
        {"model": "another-model"},
        {"ref_image_paths": ["https://example.test/reference.png"]},
    ],
)
def test_xlinks_rejects_unsupported_requests_before_network(tmp_path, kwargs) -> None:
    session = FakeSession(FakeResponse(payload={"data": []}))
    with pytest.raises(ValueError):
        _model(session).generate("prompt", str(tmp_path / "output.png"), **kwargs)
    assert session.posts == []


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"b64_json": "one"}, {"b64_json": "two"}],
        [{}],
        [{"b64_json": "one", "url": "https://cdn.example/image.png"}],
    ],
)
def test_xlinks_rejects_missing_extra_or_ambiguous_outputs(tmp_path, data) -> None:
    session = FakeSession(FakeResponse(payload={"data": data}))
    with pytest.raises(RuntimeError, match="one|exactly one"):
        _model(session).generate("prompt", str(tmp_path / "output.png"))


def test_xlinks_rejects_invalid_media_and_never_replaces_destination(tmp_path) -> None:
    encoded = base64.b64encode(b"not-a-png").decode()
    session = FakeSession(FakeResponse(payload={"data": [{"b64_json": encoded}]}))
    output = tmp_path / "existing.png"
    output.write_bytes(PNG)

    with pytest.raises(RuntimeError, match="not PNG"):
        _model(session).generate("prompt", str(output))

    assert output.read_bytes() == PNG


def test_xlinks_redacts_secret_from_provider_rejection(tmp_path) -> None:
    session = FakeSession(
        FakeResponse(
            status_code=401,
            payload={
                "error": {
                    "code": "invalid_api_key",
                    "message": "bad xlinks-test-secret",
                }
            },
        )
    )
    with pytest.raises(ProviderRequestRejectedError) as caught:
        _model(session).generate("prompt", str(tmp_path / "output.png"))
    assert "xlinks-test-secret" not in str(caught.value)
    assert caught.value.safe_error_code == "PROVIDER_AUTHENTICATION_FAILED"


def test_xlinks_logs_full_request_and_failure_response(tmp_path, caplog) -> None:
    response = FakeResponse(
        status_code=502,
        payload={
            "error": {
                "code": "upstream_generation_failed",
                "message": "image channel timed out after 85 seconds",
                "details": {"channel": "gpt-image-2-primary", "retryable": True},
            }
        },
        headers={"x-oneapi-request-id": "req-xlinks-failed"},
    )
    session = FakeSession(response)

    with caplog.at_level(logging.INFO, logger="src.models.xlinks"):
        with pytest.raises(RuntimeError, match="upstream_generation_failed"):
            _model(session).generate(
                "完整记录这个角色提示词",
                str(tmp_path / "failed.png"),
                model="gpt-image-2",
                size="1024x1024",
                quality="high",
                output_format="png",
                background="auto",
                n=1,
            )

    events = [json.loads(record.getMessage()) for record in caplog.records]
    request_event = next(
        event for event in events if event["event"] == "xlinks.image_request"
    )
    response_event = next(
        event for event in events if event["event"] == "xlinks.image_response"
    )
    assert request_event["request_body"] == {
        "model": "gpt-image-2",
        "prompt": "完整记录这个角色提示词",
        "n": 1,
        "size": "1024x1024",
        "quality": "high",
        "output_format": "png",
        "background": "auto",
    }
    assert "Authorization" not in request_event
    assert response_event["status_code"] == 502
    assert response_event["provider_request_id"] == "req-xlinks-failed"
    assert response_event["response_body"] == response._payload
    assert response_event["response_headers"] == {
        "x-oneapi-request-id": "req-xlinks-failed"
    }


def test_xlinks_success_log_omits_only_large_base64_payload(tmp_path, caplog) -> None:
    encoded = base64.b64encode(PNG).decode()
    response = FakeResponse(
        payload={
            "created": 123,
            "data": [{"b64_json": encoded, "revised_prompt": "完整响应字段"}],
        }
    )

    with caplog.at_level(logging.INFO, logger="src.models.xlinks"):
        _model(FakeSession(response)).generate(
            "prompt",
            str(tmp_path / "success.png"),
        )

    events = [json.loads(record.getMessage()) for record in caplog.records]
    response_event = next(
        event for event in events if event["event"] == "xlinks.image_response"
    )
    assert response_event["response_body"] == {
        "created": 123,
        "data": [
            {
                "b64_json": {
                    "binary_omitted": True,
                    "encoded_chars": len(encoded),
                },
                "revised_prompt": "完整响应字段",
            }
        ],
    }


def test_xlinks_logs_response_processing_failure(tmp_path, caplog) -> None:
    response = FakeResponse(
        payload={"created": 123, "data": [{}]},
        headers={"x-request-id": "req-invalid-output"},
    )

    with caplog.at_level(logging.INFO, logger="src.models.xlinks"):
        with pytest.raises(RuntimeError, match="usable output"):
            _model(FakeSession(response)).generate(
                "prompt",
                str(tmp_path / "invalid-output.png"),
            )

    events = [json.loads(record.getMessage()) for record in caplog.records]
    processing_event = next(
        event
        for event in events
        if event["event"] == "xlinks.image_processing_failed"
    )
    assert processing_event["status_code"] == 200
    assert processing_event["provider_request_id"] == "req-invalid-output"
    assert processing_event["error_type"] == "RuntimeError"
    assert processing_event["error_message"] == (
        "Xlinks image response must contain one usable output"
    )


class CredentialProvider:
    def resolve(self, secret_ref: str) -> SecretStr:
        assert secret_ref == "XLINKS_API_KEY"
        return SecretStr("factory-secret")


def _snapshot(capability="image.t2i", model="gpt-image-2") -> ModelRouteSnapshot:
    return ModelRouteSnapshot(
        config_version_id="config-1",
        route_id="route-1",
        capability=capability,
        provider="xlinks",
        provider_model_id=model,
        display_name="GPT Image 2 · Xlinks",
        parameters={"count": 1, "size": "1024x1024"},
        metering_formula={
            "kind": "image",
            "base_tokens": 0,
            "per_image_tokens": 1,
            "max_images": 1,
            "resolution_multipliers": {"1024x1024": 1},
        },
        fallback_policy={"enabled": False},
        secret_ref="XLINKS_API_KEY",
    )


def test_cloud_factory_builds_xlinks_only_for_gpt_image_t2i(monkeypatch) -> None:
    monkeypatch.setenv("XLINKS_BASE_URL", "https://api.xlinks.site/v1")
    factory = RequestScopedModelClientFactory(CredentialProvider())

    client = factory.create(_snapshot())

    assert isinstance(client.adapter, XlinksImageModel)
    assert client.adapter.api_key == "factory-secret"
    with pytest.raises(ModelClientUnavailableError, match="暂不支持能力"):
        factory.create(replace(_snapshot(), capability="image.i2i"))
    with pytest.raises(ModelClientUnavailableError, match="仅支持"):
        factory.create(replace(_snapshot(), provider_model_id="other"))


def test_xlinks_timeout_is_not_retried(tmp_path, caplog) -> None:
    class TimeoutSession(FakeSession):
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            raise requests.ReadTimeout("ambiguous timeout")

    session = TimeoutSession(None)
    with caplog.at_level(logging.INFO, logger="src.models.xlinks"):
        with pytest.raises(requests.ReadTimeout):
            _model(session).generate("prompt", str(tmp_path / "output.png"))
    assert len(session.posts) == 1
    events = [json.loads(record.getMessage()) for record in caplog.records]
    failure_event = next(
        event for event in events if event["event"] == "xlinks.image_transport_failed"
    )
    assert failure_event["request_body"]["prompt"] == "prompt"
    assert failure_event["error_type"] == "ReadTimeout"
    assert failure_event["error_message"] == "ambiguous timeout"
