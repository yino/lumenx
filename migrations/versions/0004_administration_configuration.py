"""Create versioned configuration, audit, and import administration tables."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_admin_configuration"
down_revision: str | None = "0003_execution_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version_number > 0 AND schema_version > 0", name="ck_config_versions_numbers_positive"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'superseded', 'disabled')",
            name="ck_config_versions_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_config_versions"),
        sa.UniqueConstraint("version_number", name="uq_config_versions_version_number"),
    )
    op.create_table(
        "model_configs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("config_version_id", sa.BigInteger(), nullable=False),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("display_name_zh", sa.String(length=160), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_model_id", sa.String(length=160), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("default_parameters", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("parameter_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metering_formula", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fallback_policy", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("secret_ref", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("priority >= 0", name="ck_model_configs_priority_nonnegative"),
        sa.PrimaryKeyConstraint("id", name="pk_model_configs"),
        sa.UniqueConstraint(
            "config_version_id",
            "capability",
            "provider",
            "provider_model_id",
            name="uq_model_configs_version_route",
        ),
    )
    op.create_table(
        "platform_configs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("config_version_id", sa.BigInteger(), nullable=False),
        sa.Column("tokens_per_ticket", sa.BigInteger(), nullable=False),
        sa.Column("registration_initial_grant_microtickets", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("session_idle_seconds", sa.Integer(), nullable=False),
        sa.Column("session_absolute_seconds", sa.Integer(), nullable=False),
        sa.Column("max_sessions_per_user", sa.Integer(), nullable=False),
        sa.Column("max_ai_concurrency_per_user", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "tokens_per_ticket > 0 AND registration_initial_grant_microtickets >= 0",
            name="ck_platform_configs_ticket_values",
        ),
        sa.CheckConstraint(
            "session_idle_seconds > 0 AND session_absolute_seconds >= session_idle_seconds "
            "AND max_sessions_per_user > 0 AND max_ai_concurrency_per_user > 0",
            name="ck_platform_configs_limits_positive",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_configs"),
        sa.UniqueConstraint("config_version_id", name="uq_platform_configs_version"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("target_user_id", sa.BigInteger(), nullable=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("target_type", sa.String(length=80), nullable=False),
        sa.Column("target_id", sa.String(length=160), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("before_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("correlation_id", sa.String(length=80), nullable=False),
        sa.Column("network_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("actor_admin_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("dry_run_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'dry_run', 'running', 'completed', 'failed', 'reverted')",
            name="ck_import_batches_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_batches"),
        sa.UniqueConstraint(
            "target_user_id",
            "target_workspace_id",
            "source_fingerprint",
            name="uq_import_batches_target_fingerprint",
        ),
    )


def downgrade() -> None:
    op.drop_table("import_batches")
    op.drop_table("audit_events")
    op.drop_table("platform_configs")
    op.drop_table("model_configs")
    op.drop_table("config_versions")
