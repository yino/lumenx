from __future__ import annotations

import pytest

from src.apps.comic_gen.models import PromptConfig
from src.platform.ai_request_policy import (
    ClientAIOverrideError,
    cloud_execution_payload,
    enforce_no_client_ai_overrides,
)
from tests.test_content_repositories import _script


def test_cloud_ai_override_policy_rejects_nested_routing_and_price_fields() -> None:
    for payload in (
        {"model": "user-model"},
        {"parameters": {"providerModelId": "provider-model"}},
        {"options": [{"endpoint_url": "https://untrusted.example"}]},
        {"quote": {"tokensPerTicket": 1}},
        {"pricing": {"amount": 0}},
    ):
        with pytest.raises(ClientAIOverrideError, match="不允许指定"):
            enforce_no_client_ai_overrides(payload)

    enforce_no_client_ai_overrides(
        {
            "prompt": "雨夜街道",
            "parameters": {"duration": 5, "resolution": "720p"},
            "media_ids": ["media-id"],
        }
    )


def test_cloud_execution_payload_removes_historical_model_authority() -> None:
    project = _script(
        model_settings={
            "t2i_model": "client-historical-image-model",
            "i2i_model": "client-historical-image-model",
            "image_model": "client-historical-image-model",
            "i2v_model": "client-historical-video-model",
            "r2v_model": "client-historical-video-model",
        },
        prompt_config=PromptConfig(polish_model="client-historical-llm"),
    )

    payload = cloud_execution_payload(project)

    assert "model_settings" not in payload
    assert "polish_model" not in payload["prompt_config"]
    assert payload["title"] == project.title
    assert project.model_settings.t2i_model == "client-historical-image-model"
