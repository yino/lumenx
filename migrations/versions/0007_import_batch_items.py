"""Add resumable local-import item tracking and read-only Playground history."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_import_batch_items"
down_revision: str | None = "0006_configuration_immutability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_user_rls(table_name: str) -> None:
    op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{table_name}_user_isolation" ON "{table_name}" '
        "USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint "
        "OR lumenx_is_platform_admin()) "
        "WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint "
        "OR lumenx_is_platform_admin())"
    )


def upgrade() -> None:
    op.create_table(
        "import_batch_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("item_type", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=True),
        sa.Column("target_id", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("created_by_batch", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'reused', 'failed', 'reverted')",
            name="ck_import_batch_items_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_batch_items"),
        sa.UniqueConstraint("batch_id", "item_type", "source_key", name="uq_import_batch_items_source"),
    )
    op.create_index(
        "ix_import_batch_items_batch_status",
        "import_batch_items",
        ["batch_id", "status"],
    )
    op.create_table(
        "imported_playground_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_imported_playground_history"),
        sa.UniqueConstraint("user_id", "workspace_id", "source_id", name="uq_imported_playground_history_source"),
    )
    op.create_index(
        "ix_imported_playground_history_workspace_created",
        "imported_playground_history",
        ["user_id", "workspace_id", "created_at"],
    )
    _enable_user_rls("import_batch_items")
    _enable_user_rls("imported_playground_history")


def downgrade() -> None:
    op.drop_table("imported_playground_history")
    op.drop_table("import_batch_items")
