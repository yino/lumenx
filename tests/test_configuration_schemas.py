from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.platform.configuration_schemas import (
    ConfigurationDraft,
    ModelRouteConfig,
    PlatformConfig,
    PlatformFeatureFlags,
    RegistrationMode,
)


def _platform(**updates) -> dict:
    payload = {
        "tokens_per_ticket": 1000,
        "registration_initial_grant_microtickets": 0,
        "session_idle_seconds": 3600,
        "session_absolute_seconds": 86400,
        "max_sessions_per_user": 5,
        "max_ai_concurrency_per_user": 2,
        "exposed_capabilities": ["image.t2i"],
    }
    payload.update(updates)
    return payload


def _image_route(**updates) -> dict:
    payload = {
        "capability": "image.t2i",
        "display_name_zh": "平台图像模型",
        "provider": "dashscope",
        "provider_model_id": "wan-image-v1",
        "enabled": True,
        "is_primary": True,
        "priority": 10,
        "default_parameters": {"count": 1, "resolution": "1024x1024"},
        "parameter_schema": [
            {
                "name": "count",
                "value_type": "integer",
                "required": True,
                "minimum": 1,
                "maximum": 4,
            },
            {
                "name": "resolution",
                "value_type": "string",
                "required": True,
                "choices": ["1024x1024", "1280x720"],
            },
        ],
        "metering_formula": {
            "kind": "image",
            "base_tokens": 10,
            "per_image_tokens": 100,
            "max_images": 4,
            "resolution_multipliers": {"1024x1024": 1, "1280x720": 2},
        },
        "fallback_policy": {
            "enabled": True,
            "eligible_error_codes": ["PROVIDER_UNAVAILABLE"],
            "max_attempts": 2,
        },
        "secret_ref": "DASHSCOPE_API_KEY",
    }
    payload.update(updates)
    return payload


def test_model_route_normalizes_only_allowed_parameters() -> None:
    route = ModelRouteConfig.model_validate(_image_route())

    assert route.normalize_parameters({"count": 3}) == {
        "count": 3,
        "resolution": "1024x1024",
    }
    with pytest.raises(ValueError, match="不受支持"):
        route.normalize_parameters({"provider": "other"})
    with pytest.raises(ValueError, match="超出允许范围"):
        route.normalize_parameters({"count": 5})
    with pytest.raises(ValueError, match="允许选项"):
        route.normalize_parameters({"resolution": "8k"})


def test_route_schema_rejects_unbounded_or_duplicate_configuration() -> None:
    with pytest.raises(ValidationError, match="正整数"):
        ModelRouteConfig.model_validate(
            _image_route(
                metering_formula={
                    "kind": "image",
                    "base_tokens": 0,
                    "per_image_tokens": 100,
                    "max_images": 4,
                    "resolution_multipliers": {"1024x1024": 0},
                }
            )
        )

    duplicate_parameters = _image_route()
    duplicate_parameters["parameter_schema"].append(
        {
            "name": "count",
            "value_type": "integer",
            "minimum": 1,
            "maximum": 4,
        }
    )
    with pytest.raises(ValidationError, match="不能重复"):
        ModelRouteConfig.model_validate(duplicate_parameters)

    with pytest.raises(ValidationError, match="错误码"):
        ModelRouteConfig.model_validate(
            _image_route(
                fallback_policy={
                    "enabled": True,
                    "eligible_error_codes": [],
                }
            )
        )


def test_platform_settings_validate_exchange_sessions_and_concurrency() -> None:
    platform = PlatformConfig.model_validate(_platform())
    assert platform.tokens_per_ticket == 1000
    assert platform.operational.signed_media_url_seconds == 300
    assert PlatformConfig.model_validate(
        _platform(exposed_capabilities=[])
    ).exposed_capabilities == []
    assert PlatformFeatureFlags.model_validate(
        {"registration_mode": "open"}
    ).registration_mode is RegistrationMode.OPEN

    with pytest.raises(ValidationError, match="greater than 0"):
        PlatformConfig.model_validate(_platform(tokens_per_ticket=0))
    with pytest.raises(ValidationError, match="不能短于"):
        PlatformConfig.model_validate(
            _platform(session_idle_seconds=7200, session_absolute_seconds=3600)
        )
    with pytest.raises(ValidationError, match="不能重复"):
        PlatformConfig.model_validate(
            _platform(exposed_capabilities=["image.t2i", "image.t2i"])
        )


def test_legacy_registration_boolean_migrates_fail_closed() -> None:
    assert (
        PlatformFeatureFlags.model_validate({"registration_enabled": False}).registration_mode
        is RegistrationMode.DISABLED
    )
    assert (
        PlatformFeatureFlags.model_validate({"registration_enabled": True}).registration_mode
        is RegistrationMode.INVITE_ONLY
    )
    assert "registration_enabled" not in PlatformFeatureFlags.model_validate(
        {"registration_enabled": True}
    ).model_dump(mode="json")


def test_legacy_worker_capacity_is_not_preserved_as_business_policy() -> None:
    platform = PlatformConfig.model_validate(
        _platform(
            operational={
                "signed_media_url_seconds": 120,
                "soft_delete_retention_days": 7,
                "stale_hold_minutes": 5,
                "global_worker_concurrency": 64,
            }
        )
    )
    assert "global_worker_concurrency" not in platform.operational.model_dump()


def test_configuration_draft_rejects_duplicate_routes_and_extra_fields() -> None:
    with pytest.raises(ValidationError, match="必须填写原因"):
        ConfigurationDraft.model_validate(
            {
                "reason": "   ",
                "platform": _platform(),
                "routes": [_image_route()],
            }
        )

    with pytest.raises(ValidationError, match="重复模型路由"):
        ConfigurationDraft.model_validate(
            {
                "reason": "测试重复路由",
                "platform": _platform(),
                "routes": [_image_route(), _image_route()],
            }
        )

    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelRouteConfig.model_validate(
            _image_route(api_key="plaintext-secret")
        )

    with pytest.raises(ValidationError):
        ModelRouteConfig.model_validate(
            _image_route(secret_ref="sk-plaintext-secret")
        )

    sensitive_parameter = _image_route()
    sensitive_parameter["parameter_schema"].append(
        {
            "name": "api_key",
            "value_type": "string",
        }
    )
    sensitive_parameter["default_parameters"]["api_key"] = "sk-plaintext-secret"
    with pytest.raises(ValidationError, match="凭据或密码字段"):
        ModelRouteConfig.model_validate(sensitive_parameter)
