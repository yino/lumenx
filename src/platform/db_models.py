from __future__ import annotations

from datetime import UTC, datetime

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# SQLite requires the exact INTEGER type name for rowid-backed autoincrement.
DATABASE_ID = BigInteger().with_variant(Integer, "sqlite")


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("status IN ('active', 'suspended')", name="ck_users_status"),)

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    phone_canonical: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class WorkspaceRecord(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_workspaces_version_positive"),
        UniqueConstraint("user_id", "id", name="uq_workspaces_user_id_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class AuthSessionRecord(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    configuration_version_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    network_fingerprint: Mapped[str | None] = mapped_column(String(128))
    user_agent: Mapped[str | None] = mapped_column(String(512))


class PasswordResetCredentialRecord(Base):
    __tablename__ = "password_reset_credentials"

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    created_by_admin_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RegistrationInvitationRecord(Base):
    __tablename__ = "registration_invitations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'consumed', 'revoked', 'expired')",
            name="ck_registration_invitations_status",
        ),
        CheckConstraint(
            "(status = 'consumed' AND consumed_by_user_id IS NOT NULL "
            "AND consumed_at IS NOT NULL) OR "
            "(status <> 'consumed' AND consumed_by_user_id IS NULL "
            "AND consumed_at IS NULL)",
            name="ck_registration_invitations_consumption",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    phone_canonical: Mapped[str] = mapped_column(String(32), nullable=False)
    invitation_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by_admin_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    issue_reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_by_user_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScopedDocumentMixin:
    user_id: Mapped[int]
    workspace_id: Mapped[int]
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SeriesRecord(ScopedDocumentMixin, Base):
    __tablename__ = "series"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_series_schema_version_positive"),
        CheckConstraint("version > 0", name="ck_series_version_positive"),
        UniqueConstraint("user_id", "workspace_id", "id", name="uq_series_owner_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)


class ProjectRecord(ScopedDocumentMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_projects_schema_version_positive"),
        CheckConstraint("version > 0", name="ck_projects_version_positive"),
        UniqueConstraint("user_id", "workspace_id", "id", name="uq_projects_owner_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    series_id: Mapped[int | None] = mapped_column(DATABASE_ID)


class MediaObjectRecord(Base):
    __tablename__ = "media_objects"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_media_objects_size_nonnegative"),
        CheckConstraint(
            "lifecycle_state IN ('pending', 'active', 'deleted', 'failed')",
            name="ck_media_objects_lifecycle",
        ),
        UniqueConstraint("object_key", name="uq_media_objects_object_key"),
        UniqueConstraint("user_id", "workspace_id", "id", name="uq_media_objects_owner_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    project_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    object_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AssetRecord(Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint("scope IN ('project', 'series', 'workspace', 'system')", name="ck_assets_scope"),
        CheckConstraint("asset_type IN ('character', 'scene', 'prop', 'voice', 'other')", name="ck_assets_type"),
        CheckConstraint("schema_version > 0 AND version > 0", name="ck_assets_versions_positive"),
        CheckConstraint(
            "(scope = 'system' AND user_id IS NULL AND workspace_id IS NULL) OR "
            "(scope <> 'system' AND user_id IS NOT NULL AND workspace_id IS NOT NULL)",
            name="ck_assets_scope_owner",
        ),
        CheckConstraint(
            "(scope = 'project' AND project_id IS NOT NULL AND series_id IS NULL) OR "
            "(scope = 'series' AND series_id IS NOT NULL AND project_id IS NULL) OR "
            "(scope IN ('workspace', 'system') AND project_id IS NULL AND series_id IS NULL)",
            name="ck_assets_scope_parent",
        ),
        UniqueConstraint("user_id", "workspace_id", "id", name="uq_assets_owner_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    workspace_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    project_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    series_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    media_object_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketWalletRecord(Base):
    __tablename__ = "ticket_wallets"
    __table_args__ = (
        CheckConstraint(
            "available_microtickets >= 0 AND held_microtickets >= 0 AND "
            "lifetime_granted_microtickets >= 0 AND lifetime_spent_microtickets >= 0",
            name="ck_ticket_wallets_nonnegative",
        ),
        CheckConstraint("version > 0", name="ck_ticket_wallets_version_positive"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, unique=True, nullable=False)
    available_microtickets: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    held_microtickets: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_granted_microtickets: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lifetime_spent_microtickets: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AITaskRecord(Base):
    __tablename__ = "ai_tasks"
    __table_args__ = (
        CheckConstraint("tokens_per_ticket > 0", name="ck_ai_tasks_tokens_per_ticket_positive"),
        CheckConstraint("quoted_microtickets >= 0", name="ck_ai_tasks_quote_nonnegative"),
        CheckConstraint(
            "status IN ('reserved', 'queued', 'running', 'provider_succeeded', "
            "'succeeded', 'failed', 'cancelled', 'support_review')",
            name="ck_ai_tasks_status",
        ),
        UniqueConstraint(
            "user_id",
            "idempotency_key",
            name="uq_ai_tasks_user_idempotency_key",
        ),
        UniqueConstraint("user_id", "workspace_id", "id", name="uq_ai_tasks_owner_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    project_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="reserved", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tokens_per_ticket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quoted_microtickets: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_billable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    support_review_reason: Mapped[str | None] = mapped_column(Text)
    safe_error_code: Mapped[str | None] = mapped_column(String(80))
    safe_error_message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AITaskAttemptRecord(Base):
    __tablename__ = "ai_task_attempts"
    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="ck_ai_task_attempts_number_positive"),
        CheckConstraint(
            "status IN ('pending', 'running', 'polling', 'succeeded', 'failed', 'cancelled', 'ambiguous')",
            name="ck_ai_task_attempts_status",
        ),
        UniqueConstraint("task_id", "attempt_number", name="uq_ai_task_attempts_number"),
        UniqueConstraint("task_id", "retry_key", name="uq_ai_task_attempts_retry_key"),
        UniqueConstraint("user_id", "workspace_id", "id", name="uq_ai_task_attempts_owner_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    task_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    retry_of_attempt_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    retry_key: Mapped[str | None] = mapped_column(String(160))
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_task_id: Mapped[str | None] = mapped_column(String(255))
    billable_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    diagnostic: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketHoldRecord(Base):
    __tablename__ = "ticket_holds"
    __table_args__ = (
        CheckConstraint(
            "quoted_microtickets >= 0 AND remaining_microtickets >= 0 "
            "AND remaining_microtickets <= quoted_microtickets",
            name="ck_ticket_holds_amounts",
        ),
        CheckConstraint("status IN ('held', 'settled', 'released')", name="ck_ticket_holds_status"),
        UniqueConstraint("attempt_id", name="uq_ticket_holds_attempt_id"),
        UniqueConstraint("user_id", "id", name="uq_ticket_holds_user_id_id"),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    task_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    quoted_microtickets: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remaining_microtickets: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="held", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TicketLedgerRecord(Base):
    __tablename__ = "ticket_ledger"
    __table_args__ = (
        CheckConstraint("amount_microtickets >= 0", name="ck_ticket_ledger_amount_nonnegative"),
        CheckConstraint(
            "available_after >= 0 AND held_after >= 0",
            name="ck_ticket_ledger_balances_nonnegative",
        ),
        CheckConstraint(
            "entry_type IN ('grant', 'hold', 'settlement', 'release', 'adjustment', 'compensation')",
            name="ck_ticket_ledger_entry_type",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    project_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    task_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    hold_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    actor_user_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_microtickets: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    held_delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    held_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    correlation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UsageEventRecord(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint(
            "metering_tokens >= 0 AND tokens_per_ticket > 0 AND charged_microtickets >= 0",
            name="ck_usage_events_amounts",
        ),
        CheckConstraint(
            "outcome IN ('succeeded', 'nonbillable_failure', 'billable_failure', 'cancelled')",
            name="ck_usage_events_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    project_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    task_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_provider_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    metering_formula: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metering_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tokens_per_ticket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_microtickets: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConfigVersionRecord(Base):
    __tablename__ = "config_versions"
    __table_args__ = (
        CheckConstraint(
            "version_number > 0 AND schema_version > 0",
            name="ck_config_versions_numbers_positive",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'superseded', 'disabled')",
            name="ck_config_versions_status",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    version_number: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelConfigRecord(Base):
    __tablename__ = "model_configs"
    __table_args__ = (
        CheckConstraint("priority >= 0", name="ck_model_configs_priority_nonnegative"),
        UniqueConstraint(
            "config_version_id",
            "capability",
            "provider",
            "provider_model_id",
            name="uq_model_configs_version_route",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    config_version_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    display_name_zh: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    default_parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    metering_formula: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fallback_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlatformConfigRecord(Base):
    __tablename__ = "platform_configs"
    __table_args__ = (
        CheckConstraint(
            "tokens_per_ticket > 0 AND registration_initial_grant_microtickets >= 0",
            name="ck_platform_configs_ticket_values",
        ),
        CheckConstraint(
            "session_idle_seconds > 0 AND session_absolute_seconds >= session_idle_seconds "
            "AND max_sessions_per_user > 0 AND max_ai_concurrency_per_user > 0",
            name="ck_platform_configs_limits_positive",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    config_version_id: Mapped[int] = mapped_column(DATABASE_ID, unique=True, nullable=False)
    tokens_per_ticket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    registration_initial_grant_microtickets: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False
    )
    session_idle_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    session_absolute_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_sessions_per_user: Mapped[int] = mapped_column(Integer, nullable=False)
    max_ai_concurrency_per_user: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    target_user_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    workspace_id: Mapped[int | None] = mapped_column(DATABASE_ID)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(160))
    reason: Mapped[str | None] = mapped_column(Text)
    before_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(80), nullable=False)
    network_fingerprint: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class ImportBatchRecord(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'dry_run', 'running', 'completed', 'failed', 'reverted')",
            name="ck_import_batches_status",
        ),
        UniqueConstraint(
            "target_user_id",
            "target_workspace_id",
            "source_fingerprint",
            name="uq_import_batches_target_fingerprint",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    actor_admin_user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    target_user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    target_workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    dry_run_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_reason: Mapped[str | None] = mapped_column(Text)


class ImportBatchItemRecord(Base):
    __tablename__ = "import_batch_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'reused', 'failed', 'reverted')",
            name="ck_import_batch_items_status",
        ),
        UniqueConstraint(
            "batch_id",
            "item_type",
            "source_key",
            name="uq_import_batch_items_source",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    item_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    created_by_batch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ImportedPlaygroundHistoryRecord(Base):
    __tablename__ = "imported_playground_history"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "workspace_id",
            "source_id",
            name="uq_imported_playground_history_source",
        ),
    )

    id: Mapped[int] = mapped_column(DATABASE_ID, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    workspace_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    import_batch_id: Mapped[int] = mapped_column(DATABASE_ID, nullable=False)
    source_id: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
