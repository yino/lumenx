"""Create persistent AI execution and ticket accounting tables."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003_execution_billing"
down_revision: str | None = "0002_content_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ticket_wallets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("available_microtickets", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("held_microtickets", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lifetime_granted_microtickets", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("lifetime_spent_microtickets", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "available_microtickets >= 0 AND held_microtickets >= 0 AND "
            "lifetime_granted_microtickets >= 0 AND lifetime_spent_microtickets >= 0",
            name="ck_ticket_wallets_nonnegative",
        ),
        sa.CheckConstraint("version > 0", name="ck_ticket_wallets_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_wallets"),
        sa.UniqueConstraint("user_id", name="uq_ticket_wallets_user_id"),
    )
    op.create_table(
        "ai_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="reserved", nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tokens_per_ticket", sa.BigInteger(), nullable=False),
        sa.Column("quoted_microtickets", sa.BigInteger(), nullable=False),
        sa.Column("provider_billable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("support_review_reason", sa.Text(), nullable=True),
        sa.Column("safe_error_code", sa.String(length=80), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("tokens_per_ticket > 0", name="ck_ai_tasks_tokens_per_ticket_positive"),
        sa.CheckConstraint("quoted_microtickets >= 0", name="ck_ai_tasks_quote_nonnegative"),
        sa.CheckConstraint(
            "status IN ('reserved', 'queued', 'running', 'provider_succeeded', "
            "'succeeded', 'failed', 'cancelled', 'support_review')",
            name="ck_ai_tasks_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_tasks"),
        sa.UniqueConstraint("user_id", "workspace_id", "id", name="uq_ai_tasks_owner_id"),
    )
    op.create_table(
        "ai_task_attempts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("retry_of_attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("retry_key", sa.String(length=160), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_model_id", sa.String(length=160), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("provider_task_id", sa.String(length=255), nullable=True),
        sa.Column("billable_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diagnostic", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_ai_task_attempts_number_positive"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'polling', 'succeeded', 'failed', 'cancelled', 'ambiguous')",
            name="ck_ai_task_attempts_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_task_attempts"),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_ai_task_attempts_number"),
        sa.UniqueConstraint("task_id", "retry_key", name="uq_ai_task_attempts_retry_key"),
        sa.UniqueConstraint("user_id", "workspace_id", "id", name="uq_ai_task_attempts_owner_id"),
    )
    op.create_table(
        "ticket_holds",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("quoted_microtickets", sa.BigInteger(), nullable=False),
        sa.Column("remaining_microtickets", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="held", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "quoted_microtickets >= 0 AND remaining_microtickets >= 0 "
            "AND remaining_microtickets <= quoted_microtickets",
            name="ck_ticket_holds_amounts",
        ),
        sa.CheckConstraint("status IN ('held', 'settled', 'released')", name="ck_ticket_holds_status"),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_holds"),
        sa.UniqueConstraint("attempt_id", name="uq_ticket_holds_attempt_id"),
        sa.UniqueConstraint("user_id", "id", name="uq_ticket_holds_user_id_id"),
    )
    op.create_table(
        "ticket_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=True),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("hold_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount_microtickets", sa.BigInteger(), nullable=False),
        sa.Column("available_delta", sa.BigInteger(), nullable=False),
        sa.Column("held_delta", sa.BigInteger(), nullable=False),
        sa.Column("available_after", sa.BigInteger(), nullable=False),
        sa.Column("held_after", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("correlation", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount_microtickets >= 0", name="ck_ticket_ledger_amount_nonnegative"),
        sa.CheckConstraint("available_after >= 0 AND held_after >= 0", name="ck_ticket_ledger_balances_nonnegative"),
        sa.CheckConstraint(
            "entry_type IN ('grant', 'hold', 'settlement', 'release', 'adjustment', 'compensation')",
            name="ck_ticket_ledger_entry_type",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_ledger"),
    )
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_id", sa.BigInteger(), nullable=True),
        sa.Column("capability", sa.String(length=80), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("raw_provider_usage", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("metering_formula", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metering_tokens", sa.BigInteger(), nullable=False),
        sa.Column("tokens_per_ticket", sa.BigInteger(), nullable=False),
        sa.Column("charged_microtickets", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "metering_tokens >= 0 AND tokens_per_ticket > 0 AND charged_microtickets >= 0",
            name="ck_usage_events_amounts",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'nonbillable_failure', 'billable_failure', 'cancelled')",
            name="ck_usage_events_outcome",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
    )


def downgrade() -> None:
    op.drop_table("usage_events")
    op.drop_table("ticket_ledger")
    op.drop_table("ticket_holds")
    op.drop_table("ai_task_attempts")
    op.drop_table("ai_tasks")
    op.drop_table("ticket_wallets")
