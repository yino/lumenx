from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.platform.configuration_schemas import ConfigurationDraft
from src.platform.configuration_service import ConfigurationService
from src.platform.contracts import AdminContext, UserContext
from src.platform.db_models import (
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
)
from src.platform.model_routing import (
    DatabaseModelConfigurationProvider,
    ModelFallbackNotAllowedError,
    RequestScopedModelClientFactory,
)
from tests.test_configuration_schemas import _image_route, _platform
from tests.test_content_repositories import RepositoryDatabase, _create_scope


@pytest.fixture
def routing_service():
    database = RepositoryDatabase()
    ConfigVersionRecord.__table__.create(database.engine)
    ModelConfigRecord.__table__.create(database.engine)
    PlatformConfigRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)
    admin = AdminContext(admin_id="9001", session_id="9101", username="admin")
    service = ConfigurationService(database)
    yield database, admin, service
    database.engine.dispose()


def _activate_image_routes(
    service: ConfigurationService,
    admin,
    *,
    primary_model: str,
    include_fallback: bool,
):
    routes = [
        _image_route(
            provider_model_id=primary_model,
            priority=10,
            fallback_policy={
                "enabled": include_fallback,
                "eligible_error_codes": (
                    ["PROVIDER_UNAVAILABLE"] if include_fallback else []
                ),
                "max_attempts": 2,
                "require_nonbillable_previous_attempt": True,
            },
        )
    ]
    if include_fallback:
        routes.append(
            _image_route(
                provider_model_id=f"{primary_model}-backup",
                display_name_zh="备用图像模型",
                is_primary=False,
                priority=20,
                fallback_policy={"enabled": False},
            )
        )
    stored = service.create_version(
        admin,
        ConfigurationDraft.model_validate(
            {
                "reason": f"配置 {primary_model}",
                "platform": _platform(),
                "routes": routes,
            }
        ),
    )
    return service.activate_version(admin, stored.id, reason=f"启用 {primary_model}")


def test_route_plan_selects_primary_and_only_eligible_fallback(
    routing_service,
) -> None:
    _database, admin, service = routing_service
    active = _activate_image_routes(
        service,
        admin,
        primary_model="wan-image-primary",
        include_fallback=True,
    )
    runtime_user = UserContext(user_id="2001", session_id="2101")
    provider = DatabaseModelConfigurationProvider(service, runtime_user)

    plan = provider.build_plan("image.t2i", {"count": 2})

    assert plan.config_version_id == active.id
    assert plan.tokens_per_ticket == 1000
    assert [route.provider_model_id for route in plan.routes] == [
        "wan-image-primary",
        "wan-image-primary-backup",
    ]
    assert plan.primary.parameters == {
        "count": 2,
        "resolution": "1024x1024",
    }
    with pytest.raises(TypeError):
        plan.primary.parameters["count"] = 3  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.primary.metering_formula["resolution_multipliers"][
            "1024x1024"
        ] = 99  # type: ignore[index]

    fallback = provider.select_fallback(
        plan,
        current_route_id=plan.primary.route_id,
        error_code="PROVIDER_UNAVAILABLE",
        previous_attempt_billable=False,
        completed_attempts=1,
    )
    assert fallback.provider_model_id == "wan-image-primary-backup"

    for updates, message in (
        ({"error_code": "INVALID_REQUEST"}, "错误不允许"),
        ({"previous_attempt_billable": True}, "已产生计费"),
        ({"completed_attempts": 2}, "次数已达上限"),
    ):
        arguments = {
            "current_route_id": plan.primary.route_id,
            "error_code": "PROVIDER_UNAVAILABLE",
            "previous_attempt_billable": False,
            "completed_attempts": 1,
        }
        arguments.update(updates)
        with pytest.raises(ModelFallbackNotAllowedError, match=message):
            provider.select_fallback(plan, **arguments)


def test_model_choice_selects_only_an_enabled_route_for_the_capability(
    routing_service,
) -> None:
    _database, admin, service = routing_service
    _activate_image_routes(
        service,
        admin,
        primary_model="wan-image-primary",
        include_fallback=True,
    )
    provider = DatabaseModelConfigurationProvider(
        service,
        UserContext(user_id="2001", session_id="2101"),
    )

    selected = provider.select_route(
        "image.t2i",
        {"model_choice": "wan-image-primary-backup", "count": 2},
    )
    assert selected.provider_model_id == "wan-image-primary-backup"
    assert selected.parameters == {
        "count": 2,
        "resolution": "1024x1024",
    }

    with pytest.raises(LookupError, match="未启用模型"):
        provider.select_route(
            "image.t2i",
            {"model_choice": "video-only-model", "count": 1},
        )


def test_request_scoped_client_uses_task_snapshot_and_injected_secret(
    routing_service,
    monkeypatch,
) -> None:
    _database, admin, service = routing_service
    _activate_image_routes(
        service,
        admin,
        primary_model="wan-image-v1",
        include_fallback=False,
    )
    provider = DatabaseModelConfigurationProvider(
        service,
        UserContext(user_id="2001", session_id="2101"),
    )
    original_plan = provider.build_plan("image.t2i", {"count": 2})

    _activate_image_routes(
        service,
        admin,
        primary_model="wan-image-v2",
        include_fallback=False,
    )
    assert provider.select_route("image.t2i", {}).provider_model_id == "wan-image-v2"

    class SnapshotCredentialProvider:
        def __init__(self) -> None:
            self.references: list[str] = []

        def resolve(self, secret_ref: str) -> SecretStr:
            self.references.append(secret_ref)
            return SecretStr("snapshot-secret")

    monkeypatch.setenv("DASHSCOPE_API_KEY", "mutable-environment-secret")
    credentials = SnapshotCredentialProvider()
    factory = RequestScopedModelClientFactory(credentials)

    first = factory.create(original_plan.primary)
    second = factory.create(original_plan.primary)

    assert first is not second
    assert first.adapter is not second.adapter
    assert first.snapshot.provider_model_id == "wan-image-v1"
    assert first.adapter.api_key == "snapshot-secret"
    assert first.adapter.params["model_name"] == "wan-image-v1"
    assert first.execution_parameters()["model"] == "wan-image-v1"
    assert credentials.references == ["DASHSCOPE_API_KEY", "DASHSCOPE_API_KEY"]
