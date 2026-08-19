from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .configuration_schemas import (
    AICapability,
    ConfigurationDraft,
    ImageTokenFormula,
    LLMTokenFormula,
    ModelRouteConfig,
    PlatformConfig,
    RegistrationMode,
    SpeechTokenFormula,
    VideoTokenFormula,
)
from .contracts import AdminContext, SystemContext
from .credentials import reject_plaintext_secrets
from .database import Database
from .db_models import (
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
)
from .identifiers import parse_database_id


class ConfigurationNotFoundError(LookupError):
    pass


class ConfigurationConflictError(RuntimeError):
    pass


class ConfigurationValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredConfiguration:
    id: str
    version_number: int
    status: str
    schema_version: int
    draft: ConfigurationDraft
    created_by_admin_id: str | None
    legacy_created_by_user_id: str | None
    created_at: datetime
    activated_at: datetime | None
    superseded_at: datetime | None


class ConfigurationService:
    SCHEMA_VERSION = 1
    _ACTIVATION_LOCK_ID = 1280785238

    _RUNTIME_IDENTITY = SystemContext(service_name="runtime-configuration-reader")

    def __init__(
        self,
        database: Database,
        *,
        verification_provider_available: bool = False,
    ) -> None:
        self.database = database
        self.verification_provider_available = verification_provider_available
        self._cache_lock = threading.RLock()
        self._version_cache: dict[str, StoredConfiguration] = {}

    @staticmethod
    def _require_admin(identity: AdminContext) -> None:
        if not isinstance(identity, AdminContext):
            raise PermissionError("仅平台管理员可以管理平台配置")

    @staticmethod
    def _route_record(
        version_id: int,
        route: ModelRouteConfig,
    ) -> ModelConfigRecord:
        return ModelConfigRecord(
            config_version_id=version_id,
            capability=route.capability.value,
            display_name_zh=route.display_name_zh,
            provider=route.provider,
            provider_model_id=route.provider_model_id,
            enabled=route.enabled,
            is_primary=route.is_primary,
            priority=route.priority,
            default_parameters=dict(route.default_parameters),
            parameter_schema=[
                rule.model_dump(mode="json") for rule in route.parameter_schema
            ],
            metering_formula=route.metering_formula.model_dump(mode="json"),
            fallback_policy=route.fallback_policy.model_dump(mode="json"),
            secret_ref=route.secret_ref,
        )

    @staticmethod
    def _platform_record(
        version_id: int,
        platform: PlatformConfig,
    ) -> PlatformConfigRecord:
        return PlatformConfigRecord(
            config_version_id=version_id,
            tokens_per_ticket=platform.tokens_per_ticket,
            registration_initial_grant_microtickets=(
                platform.registration_initial_grant_microtickets
            ),
            session_idle_seconds=platform.session_idle_seconds,
            session_absolute_seconds=platform.session_absolute_seconds,
            max_sessions_per_user=platform.max_sessions_per_user,
            max_ai_concurrency_per_user=platform.max_ai_concurrency_per_user,
            settings={
                "exposed_capabilities": [
                    capability.value for capability in platform.exposed_capabilities
                ],
                "feature_flags": platform.feature_flags.model_dump(mode="json"),
                "operational": platform.operational.model_dump(mode="json"),
            },
        )

    @staticmethod
    def _route_from_record(record: ModelConfigRecord) -> ModelRouteConfig:
        return ModelRouteConfig.model_validate(
            {
                "capability": record.capability,
                "display_name_zh": record.display_name_zh,
                "provider": record.provider,
                "provider_model_id": record.provider_model_id,
                "enabled": record.enabled,
                "is_primary": record.is_primary,
                "priority": record.priority,
                "default_parameters": dict(record.default_parameters),
                "parameter_schema": list(record.parameter_schema),
                "metering_formula": dict(record.metering_formula),
                "fallback_policy": dict(record.fallback_policy),
                "secret_ref": record.secret_ref,
            }
        )

    @staticmethod
    def _platform_from_record(record: PlatformConfigRecord) -> PlatformConfig:
        settings = dict(record.settings)
        return PlatformConfig.model_validate(
            {
                "tokens_per_ticket": record.tokens_per_ticket,
                "registration_initial_grant_microtickets": (
                    record.registration_initial_grant_microtickets
                ),
                "session_idle_seconds": record.session_idle_seconds,
                "session_absolute_seconds": record.session_absolute_seconds,
                "max_sessions_per_user": record.max_sessions_per_user,
                "max_ai_concurrency_per_user": (
                    record.max_ai_concurrency_per_user
                ),
                "exposed_capabilities": settings.get("exposed_capabilities", []),
                "feature_flags": settings.get("feature_flags", {}),
                "operational": settings.get("operational", {}),
            }
        )

    def _load(
        self,
        session: Session,
        record: ConfigVersionRecord,
    ) -> StoredConfiguration:
        platform_record = session.scalar(
            select(PlatformConfigRecord).where(
                PlatformConfigRecord.config_version_id == record.id
            )
        )
        if platform_record is None:
            raise ConfigurationValidationError("配置版本缺少平台设置")
        route_records = list(
            session.scalars(
                select(ModelConfigRecord)
                .where(ModelConfigRecord.config_version_id == record.id)
                .order_by(
                    ModelConfigRecord.capability.asc(),
                    ModelConfigRecord.priority.asc(),
                    ModelConfigRecord.id.asc(),
                )
            )
        )
        draft = ConfigurationDraft(
            reason=record.reason,
            platform=self._platform_from_record(platform_record),
            routes=[self._route_from_record(route) for route in route_records],
        )
        return StoredConfiguration(
            id=str(record.id),
            version_number=record.version_number,
            status=record.status,
            schema_version=record.schema_version,
            draft=draft,
            created_by_admin_id=(
                str(record.created_by_admin_id)
                if record.created_by_admin_id is not None
                else None
            ),
            legacy_created_by_user_id=(
                str(record.created_by_user_id)
                if record.created_by_user_id is not None
                else None
            ),
            created_at=record.created_at,
            activated_at=record.activated_at,
            superseded_at=record.superseded_at,
        )

    @staticmethod
    def _audit_summary(configuration: StoredConfiguration) -> dict[str, Any]:
        return {
            "version_number": configuration.version_number,
            "status": configuration.status,
            "route_count": len(configuration.draft.routes),
            "feature_flags": configuration.draft.platform.feature_flags.model_dump(
                mode="json"
            ),
            "enabled_capabilities": sorted(
                {
                    route.capability.value
                    for route in configuration.draft.routes
                    if route.enabled
                }
            ),
        }

    @staticmethod
    def _add_audit(
        session: Session,
        identity: AdminContext,
        *,
        action: str,
        target_id: str,
        reason: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        correlation_id: str | None,
    ) -> None:
        reject_plaintext_secrets(before, path="审计变更前摘要")
        reject_plaintext_secrets(after, path="审计变更后摘要")
        session.add(
            AuditEventRecord(
                actor_admin_id=parse_database_id(identity.admin_id, field="管理员 ID"),
                action=action,
                target_type="configuration_version",
                target_id=target_id,
                reason=reason,
                before_summary=before,
                after_summary=after,
                correlation_id=correlation_id or str(uuid.uuid4()),
            )
        )

    def create_version(
        self,
        identity: AdminContext,
        draft: ConfigurationDraft,
        *,
        correlation_id: str | None = None,
        audit_action: str = "configuration.create",
        audit_metadata: dict[str, Any] | None = None,
    ) -> StoredConfiguration:
        self._require_admin(identity)
        try:
            with self.database.transaction(identity) as session:
                next_number = (
                    session.scalar(select(func.max(ConfigVersionRecord.version_number)))
                    or 0
                ) + 1
                record = ConfigVersionRecord(
                    version_number=next_number,
                    status="draft",
                    schema_version=self.SCHEMA_VERSION,
                    created_by_admin_id=parse_database_id(
                        identity.admin_id,
                        field="管理员 ID",
                    ),
                    reason=draft.reason,
                )
                session.add(record)
                session.flush()
                session.add(self._platform_record(record.id, draft.platform))
                session.add_all(
                    [self._route_record(record.id, route) for route in draft.routes]
                )
                session.flush()
                stored = self._load(session, record)
                audit_summary = self._audit_summary(stored)
                if audit_metadata:
                    audit_summary.update(copy.deepcopy(audit_metadata))
                self._add_audit(
                    session,
                    identity,
                    action=audit_action,
                    target_id=stored.id,
                    reason=draft.reason,
                    before=None,
                    after=audit_summary,
                    correlation_id=correlation_id,
                )
            return stored
        except IntegrityError as exc:
            raise ConfigurationConflictError(
                "配置版本号冲突，请重试"
            ) from exc

    @staticmethod
    def _formula_matches_capability(route: ModelRouteConfig) -> bool:
        if route.capability in {
            AICapability.SCRIPT_ANALYSIS,
            AICapability.PROMPT_POLISH,
        }:
            return isinstance(route.metering_formula, LLMTokenFormula)
        if route.capability in {
            AICapability.IMAGE_T2I,
            AICapability.IMAGE_I2I,
        }:
            return isinstance(route.metering_formula, ImageTokenFormula)
        if route.capability in {
            AICapability.VIDEO_T2V,
            AICapability.VIDEO_I2V,
            AICapability.VIDEO_R2V,
            AICapability.VIDEO_V2V,
        }:
            return isinstance(route.metering_formula, VideoTokenFormula)
        return isinstance(route.metering_formula, SpeechTokenFormula)

    def validate_for_activation(
        self,
        configuration: StoredConfiguration,
    ) -> None:
        errors: list[str] = []
        enabled = [route for route in configuration.draft.routes if route.enabled]
        for capability in configuration.draft.platform.exposed_capabilities:
            routes = [route for route in enabled if route.capability == capability]
            primaries = [route for route in routes if route.is_primary]
            if len(primaries) != 1:
                errors.append(
                    f"能力 {capability.value} 必须且只能有一个启用的主路由"
                )
            priorities = [route.priority for route in routes]
            if len(priorities) != len(set(priorities)):
                errors.append(f"能力 {capability.value} 的启用路由优先级不能重复")
            if len(routes) > 1 and primaries and not primaries[0].fallback_policy.enabled:
                errors.append(f"能力 {capability.value} 配置备用路由时必须启用回退策略")
        unexpected = {
            route.capability
            for route in enabled
            if route.capability not in configuration.draft.platform.exposed_capabilities
        }
        if unexpected:
            errors.append(
                "存在未开放能力的启用路由："
                + ", ".join(sorted(capability.value for capability in unexpected))
            )
        for route in enabled:
            if not self._formula_matches_capability(route):
                errors.append(
                    f"路由 {route.provider_model_id} 的计量公式与能力不匹配"
                )
            if route.metering_formula.review_required:
                errors.append(
                    f"路由 {route.provider_model_id} 的计量公式尚未完成审核"
                )
        if (
            configuration.draft.platform.feature_flags.registration_mode
            is RegistrationMode.VERIFIED_OPEN
            and not self.verification_provider_available
        ):
            errors.append("验证码服务尚未启用，不能开放手机号自助注册")
        if errors:
            raise ConfigurationValidationError("；".join(errors))

    def get_version(
        self,
        identity: AdminContext,
        version_id: str,
    ) -> StoredConfiguration:
        self._require_admin(identity)
        try:
            canonical_id = parse_database_id(version_id, field="配置版本 ID")
        except ValueError as exc:
            raise ConfigurationNotFoundError("配置版本不存在") from exc
        with self.database.transaction(identity) as session:
            record = session.get(ConfigVersionRecord, canonical_id)
            if record is None:
                raise ConfigurationNotFoundError("配置版本不存在")
            return self._load(session, record)

    def list_versions(self, identity: AdminContext) -> list[StoredConfiguration]:
        self._require_admin(identity)
        with self.database.transaction(identity) as session:
            records = list(
                session.scalars(
                    select(ConfigVersionRecord).order_by(
                        ConfigVersionRecord.version_number.desc()
                    )
                )
            )
            return [self._load(session, record) for record in records]

    def validate_version(
        self,
        identity: AdminContext,
        version_id: str,
    ) -> StoredConfiguration:
        configuration = self.get_version(identity, version_id)
        if configuration.status == "disabled":
            raise ConfigurationConflictError("已停用的配置版本不能校验")
        self.validate_for_activation(configuration)
        return configuration

    def activate_version(
        self,
        identity: AdminContext,
        version_id: str,
        *,
        reason: str,
        correlation_id: str | None = None,
    ) -> StoredConfiguration:
        self._require_admin(identity)
        reason = reason.strip()
        if not reason:
            raise ConfigurationValidationError("激活配置必须填写原因")
        try:
            canonical_id = parse_database_id(version_id, field="配置版本 ID")
        except ValueError as exc:
            raise ConfigurationNotFoundError("配置版本不存在") from exc
        now = datetime.now(UTC)
        with self.database.transaction(identity) as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": self._ACTIVATION_LOCK_ID},
                )
            target = session.scalar(
                select(ConfigVersionRecord)
                .where(ConfigVersionRecord.id == canonical_id)
                .with_for_update()
            )
            if target is None:
                raise ConfigurationNotFoundError("配置版本不存在")
            if target.status != "draft":
                raise ConfigurationConflictError("仅草稿配置可以激活")
            candidate = self._load(session, target)
            self.validate_for_activation(candidate)
            current = session.scalar(
                select(ConfigVersionRecord)
                .where(ConfigVersionRecord.status == "active")
                .with_for_update()
            )
            before = self._load(session, current) if current is not None else None
            if current is not None:
                current.status = "superseded"
                current.superseded_at = now
                session.flush()
            target.status = "active"
            target.activated_at = now
            session.flush()
            activated = self._load(session, target)
            self._add_audit(
                session,
                identity,
                action="configuration.activate",
                target_id=activated.id,
                reason=reason,
                before=self._audit_summary(before) if before else None,
                after=self._audit_summary(activated),
                correlation_id=correlation_id,
            )
        return activated

    def disable_version(
        self,
        identity: AdminContext,
        version_id: str,
        *,
        reason: str,
        correlation_id: str | None = None,
    ) -> StoredConfiguration:
        self._require_admin(identity)
        reason = reason.strip()
        if not reason:
            raise ConfigurationValidationError("停用配置必须填写原因")
        try:
            canonical_id = parse_database_id(version_id, field="配置版本 ID")
        except ValueError as exc:
            raise ConfigurationNotFoundError("配置版本不存在") from exc
        with self.database.transaction(identity) as session:
            target = session.scalar(
                select(ConfigVersionRecord)
                .where(ConfigVersionRecord.id == canonical_id)
                .with_for_update()
            )
            if target is None:
                raise ConfigurationNotFoundError("配置版本不存在")
            if target.status != "draft":
                raise ConfigurationConflictError("仅草稿配置可以停用")
            before = self._load(session, target)
            target.status = "disabled"
            session.flush()
            disabled = self._load(session, target)
            self._add_audit(
                session,
                identity,
                action="configuration.disable",
                target_id=disabled.id,
                reason=reason,
                before=self._audit_summary(before),
                after=self._audit_summary(disabled),
                correlation_id=correlation_id,
            )
        return disabled

    def _get_active(
        self,
        identity: AdminContext | SystemContext,
    ) -> StoredConfiguration:
        with self.database.transaction(identity) as session:
            active_id = session.scalar(
                select(ConfigVersionRecord.id).where(
                    ConfigVersionRecord.status == "active"
                )
            )
            if active_id is None:
                raise ConfigurationNotFoundError("当前没有已激活的平台配置")
            cache_key = str(active_id)
            with self._cache_lock:
                cached = self._version_cache.get(cache_key)
            if cached is not None:
                return copy.deepcopy(cached)
            record = session.get(ConfigVersionRecord, active_id)
            if record is None:
                raise ConfigurationNotFoundError("当前没有已激活的平台配置")
            loaded = self._load(session, record)
        with self._cache_lock:
            self._version_cache[loaded.id] = loaded
            return copy.deepcopy(loaded)

    def get_active(self, identity: AdminContext) -> StoredConfiguration:
        self._require_admin(identity)
        return self._get_active(identity)

    def get_active_for_runtime(
        self,
        identity: SystemContext,
    ) -> StoredConfiguration:
        """Load active server routing without granting configuration administration."""
        del identity
        return self._get_active(self._RUNTIME_IDENTITY)

    def rollback_to_new_version(
        self,
        identity: AdminContext,
        source_version_id: str,
        *,
        reason: str,
        correlation_id: str | None = None,
    ) -> StoredConfiguration:
        self._require_admin(identity)
        reason = reason.strip()
        if not reason:
            raise ConfigurationValidationError("回滚配置必须填写原因")
        source = self.get_version(identity, source_version_id)
        copied_draft = source.draft.model_copy(deep=True)
        copied_draft.reason = reason
        created = self.create_version(
            identity,
            copied_draft,
            correlation_id=correlation_id,
        )
        return self.activate_version(
            identity,
            created.id,
            reason=reason,
            correlation_id=correlation_id,
        )
