from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from .configuration_schemas import RegistrationMode
from .contracts import UserContext
from .database import Database
from .db_models import ConfigVersionRecord, PlatformConfigRecord


class CloudFeatureDisabledError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CloudFeatureState:
    registration_mode: RegistrationMode
    new_ai_tasks_enabled: bool
    config_version_id: str | None

    @property
    def registration_enabled(self) -> bool:
        return self.registration_mode is not RegistrationMode.DISABLED


class CloudFeatureGate:
    """Fail-closed rollout gates backed by the active platform configuration."""

    _SYSTEM_IDENTITY = UserContext(
        user_id="0",
        session_id="cloud-feature-gate",
        is_platform_admin=True,
    )

    def __init__(
        self,
        database: Database,
        *,
        registration_emergency_disabled: bool = False,
        new_ai_tasks_emergency_disabled: bool = False,
    ) -> None:
        self.database = database
        self.registration_emergency_disabled = registration_emergency_disabled
        self.new_ai_tasks_emergency_disabled = new_ai_tasks_emergency_disabled

    def state(self) -> CloudFeatureState:
        with self.database.transaction(self._SYSTEM_IDENTITY) as session:
            record = session.scalar(
                select(PlatformConfigRecord)
                .join(
                    ConfigVersionRecord,
                    ConfigVersionRecord.id == PlatformConfigRecord.config_version_id,
                )
                .where(ConfigVersionRecord.status == "active")
                .limit(1)
            )
            if record is None:
                return CloudFeatureState(RegistrationMode.DISABLED, False, None)
            flags = dict(record.settings or {}).get("feature_flags", {})
            raw_registration_mode = flags.get("registration_mode")
            if raw_registration_mode is None:
                raw_registration_mode = (
                    RegistrationMode.INVITE_ONLY.value
                    if bool(flags.get("registration_enabled", False))
                    else RegistrationMode.DISABLED.value
                )
            try:
                registration_mode = RegistrationMode(raw_registration_mode)
            except (TypeError, ValueError):
                registration_mode = RegistrationMode.DISABLED
            if self.registration_emergency_disabled:
                registration_mode = RegistrationMode.DISABLED
            return CloudFeatureState(
                registration_mode=registration_mode,
                new_ai_tasks_enabled=(
                    bool(flags.get("new_ai_tasks_enabled", False))
                    and not self.new_ai_tasks_emergency_disabled
                ),
                config_version_id=str(record.config_version_id),
            )

    def require_registration(self) -> CloudFeatureState:
        state = self.state()
        if not state.registration_enabled:
            raise CloudFeatureDisabledError(
                "REGISTRATION_DISABLED",
                "注册暂未开放，请稍后再试",
            )
        return state

    def require_new_ai_task(self) -> None:
        if not self.state().new_ai_tasks_enabled:
            raise CloudFeatureDisabledError(
                "AI_NEW_TASKS_DISABLED",
                "AI 新任务已暂停，已有任务仍可查询和处理",
            )
