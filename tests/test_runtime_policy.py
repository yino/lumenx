from __future__ import annotations

from dataclasses import replace

from src.platform.configuration_schemas import (
    PlatformConfig,
    PlatformFeatureFlags,
    PlatformOperationalSettings,
    RegistrationMode,
)
from src.platform.configuration_service import ConfigurationService
from src.platform.runtime_policy import (
    POLICY_SOURCE_REGISTRY,
    RUNTIME_POLICY_CONSUMERS,
    PolicyAuthority,
    RuntimePolicyResolver,
)
from tests.test_configuration_service import _draft
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from src.platform.db_models import (
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
)


def _configuration_database():
    database = RepositoryDatabase()
    ConfigVersionRecord.__table__.create(database.engine)
    ModelConfigRecord.__table__.create(database.engine)
    PlatformConfigRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    return database


def test_active_policy_is_typed_and_version_aware() -> None:
    database = _configuration_database()
    context = _create_scope(database)
    admin = replace(context.identity, is_platform_admin=True)
    writer = ConfigurationService(database)
    resolver = RuntimePolicyResolver(database)
    draft = _draft(reason="运行策略第一版")
    platform = draft.platform.model_copy(
        update={
            "registration_initial_grant_microtickets": 3_000_000,
            "session_idle_seconds": 1800,
            "session_absolute_seconds": 7200,
            "max_sessions_per_user": 2,
            "max_ai_concurrency_per_user": 3,
            "feature_flags": draft.platform.feature_flags.model_copy(
                update={"registration_mode": RegistrationMode.INVITE_ONLY}
            ),
            "operational": draft.platform.operational.model_copy(
                update={
                    "signed_media_url_seconds": 90,
                    "soft_delete_retention_days": 14,
                    "stale_hold_minutes": 12,
                }
            ),
        }
    )
    first = writer.create_version(admin, draft.model_copy(update={"platform": platform}))
    writer.activate_version(admin, first.id, reason="启用运行策略")

    snapshot = resolver.resolve()

    assert snapshot.config_version_id == first.id
    assert snapshot.registration_mode.value == "invite_only"
    assert snapshot.registration_initial_grant_microtickets == 3_000_000
    assert snapshot.session_policy.idle_seconds == 1800
    assert snapshot.session_policy.absolute_seconds == 7200
    assert snapshot.max_sessions_per_user == 2
    assert snapshot.max_ai_concurrency_per_user == 3
    assert snapshot.signed_media_url_seconds == 90
    assert snapshot.soft_delete_retention_days == 14
    assert snapshot.stale_hold_minutes == 12
    database.engine.dispose()


def test_every_editable_business_policy_has_authority_and_runtime_consumer() -> None:
    editable_fields = (
        set(PlatformConfig.model_fields)
        - {"feature_flags", "operational"}
    ) | set(PlatformFeatureFlags.model_fields) | set(PlatformOperationalSettings.model_fields)

    assert editable_fields == set(RUNTIME_POLICY_CONSUMERS)
    assert all(
        POLICY_SOURCE_REGISTRY[field] is PolicyAuthority.DATABASE
        for field in editable_fields
    )
    assert "global_worker_concurrency" not in editable_fields
    assert POLICY_SOURCE_REGISTRY["worker_concurrency"] is PolicyAuthority.DEPLOYMENT
