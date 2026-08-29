from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from .configuration_schemas import RegistrationMode
from .contracts import SystemContext
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
    short_drama_agent_enabled: bool = False
    short_drama_agent_shadow_mode: bool = False
    short_drama_agent_require_approval: bool = True
    short_drama_agent_profiles: tuple[str, ...] = ()
    short_drama_agent_max_concurrency: int = 2

    @property
    def registration_enabled(self) -> bool:
        return self.registration_mode is not RegistrationMode.DISABLED


class CloudFeatureGate:
    """Fail-closed rollout gates backed by the active platform configuration."""

    _SYSTEM_IDENTITY = SystemContext(service_name="cloud-feature-gate")

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
                short_drama_agent_enabled=bool(flags.get("short_drama_agent_enabled", False)),
                short_drama_agent_shadow_mode=bool(flags.get("short_drama_agent_shadow_mode", False)),
                short_drama_agent_require_approval=bool(flags.get("short_drama_agent_require_approval", True)),
                short_drama_agent_profiles=tuple(str(item) for item in (flags.get("short_drama_agent_profiles") or ())),
                short_drama_agent_max_concurrency=int(flags.get("short_drama_agent_max_concurrency", 2)),
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

    def require_short_drama_agent(self, profile: str | None = None) -> CloudFeatureState:
        state = self.state()
        if not state.short_drama_agent_enabled:
            raise CloudFeatureDisabledError(
                "SHORT_DRAMA_AGENT_DISABLED",
                "短剧 Agent 暂未开放",
            )
        if profile and state.short_drama_agent_profiles and profile not in state.short_drama_agent_profiles:
            raise CloudFeatureDisabledError(
                "SHORT_DRAMA_AGENT_PROFILE_DISABLED",
                "当前短剧 Agent 模型未在灰度名单中",
            )
        return state
