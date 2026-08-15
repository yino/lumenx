from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .auth.sessions import SessionPolicy
from .configuration_schemas import RegistrationMode
from .configuration_service import (
    ConfigurationNotFoundError,
    ConfigurationService,
)
from .contracts import UserContext
from .database import Database


class PolicyAuthority(str, Enum):
    DATABASE = "active_postgresql_configuration"
    SNAPSHOT = "immutable_operation_snapshot"
    DEPLOYMENT = "environment_or_compose"


POLICY_SOURCE_REGISTRY: Mapping[str, PolicyAuthority] = MappingProxyType(
    {
        "tokens_per_ticket": PolicyAuthority.DATABASE,
        "registration_mode": PolicyAuthority.DATABASE,
        "registration_initial_grant_microtickets": PolicyAuthority.DATABASE,
        "session_idle_seconds": PolicyAuthority.DATABASE,
        "session_absolute_seconds": PolicyAuthority.DATABASE,
        "max_sessions_per_user": PolicyAuthority.DATABASE,
        "max_ai_concurrency_per_user": PolicyAuthority.DATABASE,
        "exposed_capabilities": PolicyAuthority.DATABASE,
        "new_ai_tasks_enabled": PolicyAuthority.DATABASE,
        "signed_media_url_seconds": PolicyAuthority.DATABASE,
        "soft_delete_retention_days": PolicyAuthority.DATABASE,
        "stale_hold_minutes": PolicyAuthority.DATABASE,
        "model_route": PolicyAuthority.SNAPSHOT,
        "metering_formula": PolicyAuthority.SNAPSHOT,
        "session_expiry": PolicyAuthority.SNAPSHOT,
        "database_url": PolicyAuthority.DEPLOYMENT,
        "redis_url": PolicyAuthority.DEPLOYMENT,
        "object_store": PolicyAuthority.DEPLOYMENT,
        "provider_credentials": PolicyAuthority.DEPLOYMENT,
        "worker_concurrency": PolicyAuthority.DEPLOYMENT,
        "trusted_proxies": PolicyAuthority.DEPLOYMENT,
    }
)


RUNTIME_POLICY_CONSUMERS: Mapping[str, str] = MappingProxyType(
    {
        "tokens_per_ticket": "DatabaseModelConfigurationProvider.build_plan",
        "registration_mode": "RegistrationService.register",
        "registration_initial_grant_microtickets": "RegistrationService.register",
        "session_idle_seconds": "AuthenticationService.login",
        "session_absolute_seconds": "AuthenticationService.login",
        "max_sessions_per_user": "AuthenticationService.login",
        "max_ai_concurrency_per_user": "TicketReservationService.reserve",
        "exposed_capabilities": "DatabaseModelConfigurationProvider.build_plan",
        "new_ai_tasks_enabled": "CloudFeatureGate.require_new_ai_task",
        "signed_media_url_seconds": "install_cloud_media_api.get_media_access",
        "soft_delete_retention_days": "RetentionCleanupService.run",
        "stale_hold_minutes": "TaskHoldMaintenanceService.run",
    }
)


class RuntimePolicyUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimePolicySnapshot:
    config_version_id: str
    registration_mode: RegistrationMode
    verification_available: bool
    registration_initial_grant_microtickets: int
    session_policy: SessionPolicy
    max_sessions_per_user: int
    max_ai_concurrency_per_user: int
    tokens_per_ticket: int
    signed_media_url_seconds: int
    soft_delete_retention_days: int
    stale_hold_minutes: int
    new_ai_tasks_enabled: bool

    @property
    def registration_enabled(self) -> bool:
        return self.registration_mode is not RegistrationMode.DISABLED


class RuntimePolicyResolver:
    _IDENTITY = UserContext(
        user_id="00000000-0000-0000-0000-000000000000",
        session_id="runtime-policy-resolver",
        is_platform_admin=True,
    )

    def __init__(
        self,
        database: Database,
        *,
        verification_available: bool = False,
        configuration: ConfigurationService | None = None,
    ) -> None:
        self.verification_available = verification_available
        self.configuration = configuration or ConfigurationService(
            database,
            verification_provider_available=verification_available,
        )

    def resolve(self) -> RuntimePolicySnapshot:
        try:
            active = self.configuration.get_active_for_runtime(self._IDENTITY)
        except ConfigurationNotFoundError as exc:
            raise RuntimePolicyUnavailableError("平台运行策略尚未激活") from exc
        platform = active.draft.platform
        mode = platform.feature_flags.registration_mode
        if mode is RegistrationMode.VERIFIED_OPEN and not self.verification_available:
            raise RuntimePolicyUnavailableError("验证码服务不可用，开放注册策略无效")
        return RuntimePolicySnapshot(
            config_version_id=active.id,
            registration_mode=mode,
            verification_available=self.verification_available,
            registration_initial_grant_microtickets=(
                platform.registration_initial_grant_microtickets
            ),
            session_policy=SessionPolicy(
                idle_seconds=platform.session_idle_seconds,
                absolute_seconds=platform.session_absolute_seconds,
            ),
            max_sessions_per_user=platform.max_sessions_per_user,
            max_ai_concurrency_per_user=platform.max_ai_concurrency_per_user,
            tokens_per_ticket=platform.tokens_per_ticket,
            signed_media_url_seconds=platform.operational.signed_media_url_seconds,
            soft_delete_retention_days=platform.operational.soft_delete_retention_days,
            stale_hold_minutes=platform.operational.stale_hold_minutes,
            new_ai_tasks_enabled=platform.feature_flags.new_ai_tasks_enabled,
        )
