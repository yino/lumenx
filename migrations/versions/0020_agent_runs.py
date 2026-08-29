"""Add scoped generic Agent Run snapshots."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_agent_runs"
down_revision: str | None = "0019_recharge_event_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.String(length=120), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("current_stage", sa.String(length=64), nullable=False, server_default="intake"),
        sa.Column("stage_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("schema_version > 0", name="ck_agent_runs_schema_version_positive"),
        sa.CheckConstraint("status IN ('queued','running','needs_approval','blocked','failed','completed','cancelled')", name="ck_agent_runs_status"),
        sa.UniqueConstraint("run_id", name="uq_agent_runs_run_id"),
        sa.UniqueConstraint("user_id", "workspace_id", "idempotency_key", name="uq_agent_runs_idempotency"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
    )
    op.create_index("ix_agent_runs_scope", "agent_runs", ["user_id", "workspace_id", "project_id"])
    op.execute('ALTER TABLE "agent_runs" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "agent_runs" FORCE ROW LEVEL SECURITY')
    op.execute(
        """
        CREATE POLICY agent_runs_user_scope ON agent_runs
        USING (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        WITH CHECK (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "agent_runs_user_scope" ON "agent_runs"')
    op.drop_index("ix_agent_runs_scope", table_name="agent_runs")
    op.drop_table("agent_runs")
