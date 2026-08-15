from __future__ import annotations

from dataclasses import replace

import pytest

from src.platform.ai_gateway_contract import (
    AIGatewayContractError,
    AIGatewayRequestContract,
)
from src.platform.contracts import UserContext, WorkspaceContext


def _context() -> WorkspaceContext:
    return WorkspaceContext(
        identity=UserContext(
            user_id="1",
            session_id="1",
        ),
        workspace_id="2",
    )


def _payload(**updates):
    payload = {
        "capability": "image.t2i",
        "idempotency_key": "generate-image-001",
        "project_id": "3",
        "resource_ids": {"asset": ["4"]},
        "media_ids": [],
        "content": "  雨夜中的未来城市  ",
        "parameters": {"count": 1, "resolution": "1024x1024"},
    }
    payload.update(updates)
    return payload


def test_authenticated_gateway_contract_normalizes_owned_identifier_request() -> None:
    request = AIGatewayRequestContract.parse(_context(), _payload())

    assert request.capability.value == "image.t2i"
    assert request.content == "雨夜中的未来城市"
    assert request.project_id is not None
    reservation = request.reservation_payload()
    assert reservation["content"] == "雨夜中的未来城市"
    assert reservation["parameters"] == {"count": 1, "resolution": "1024x1024"}
    assert reservation["input_media_ids"] == []
    assert len(reservation["resource_ids"]["asset"]) == 1
    assert "user_id" not in reservation
    assert "workspace_id" not in reservation


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "other"},
        {"parameters": {"modelId": "client-model"}},
        {"content": {"routing": {"price": 0}}},
        {"content": {"secret_ref": "CLIENT_KEY"}},
    ],
)
def test_gateway_contract_rejects_client_routing_and_billing_overrides(override) -> None:
    payload = _payload()
    payload.update(override)

    with pytest.raises(AIGatewayContractError) as captured:
        AIGatewayRequestContract.parse(_context(), payload)

    assert captured.value.code == "AI_OVERRIDE_FORBIDDEN"
    assert captured.value.paths


@pytest.mark.parametrize(
    "forbidden_content",
    [
        {"image_url": "https://example.invalid/input.png"},
        {"source": {"object_key": "users/other/file.png"}},
        {"file_path": "/tmp/private.png"},
    ],
)
def test_gateway_contract_rejects_paths_object_keys_and_remote_urls(
    forbidden_content,
) -> None:
    with pytest.raises(AIGatewayContractError) as captured:
        AIGatewayRequestContract.parse(
            _context(),
            _payload(content=forbidden_content),
        )

    assert captured.value.code == "AI_REFERENCE_FORBIDDEN"
    assert captured.value.paths


def test_gateway_contract_requires_authenticated_context_and_server_owned_scope() -> None:
    context = _context()
    with pytest.raises(AIGatewayContractError) as captured:
        AIGatewayRequestContract.parse(
            replace(
                context,
                identity=replace(context.identity, session_id=None),
            ),
            _payload(),
        )
    assert captured.value.code == "AUTH_REQUIRED"

    with pytest.raises(AIGatewayContractError) as captured:
        AIGatewayRequestContract.parse(
            context,
            _payload(user_id="6"),
        )
    assert captured.value.code == "AI_REQUEST_INVALID"


def test_gateway_contract_validates_media_requirements_ids_and_idempotency() -> None:
    with pytest.raises(AIGatewayContractError, match="输入媒体标识"):
        AIGatewayRequestContract.parse(
            _context(),
            _payload(capability="video.i2v"),
        )

    media_id = "5"
    with pytest.raises(AIGatewayContractError, match="不能重复"):
        AIGatewayRequestContract.parse(
            _context(),
            _payload(media_ids=[media_id, media_id]),
        )

    with pytest.raises(AIGatewayContractError, match="幂等键格式无效"):
        AIGatewayRequestContract.parse(
            _context(),
            _payload(idempotency_key="invalid key"),
        )


def test_gateway_contract_rejects_credential_shaped_nested_values() -> None:
    with pytest.raises(AIGatewayContractError):
        AIGatewayRequestContract.parse(
            _context(),
            _payload(content={"authorization": "Bearer secret-value"}),
        )
