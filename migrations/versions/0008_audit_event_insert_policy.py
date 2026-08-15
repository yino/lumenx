"""Allow scoped application audit inserts while keeping audit reads admin-only."""

from collections.abc import Sequence

from alembic import op


revision: str = "0008_audit_event_insert_policy"
down_revision: str | None = "0007_import_batch_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP POLICY IF EXISTS audit_events_admin_scope ON audit_events")
    op.execute(
        "CREATE POLICY audit_events_admin_select ON audit_events FOR SELECT "
        "USING (lumenx_is_platform_admin())"
    )
    op.execute(
        "CREATE POLICY audit_events_scoped_insert ON audit_events FOR INSERT "
        "WITH CHECK (actor_user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS audit_events_scoped_insert ON audit_events")
    op.execute("DROP POLICY IF EXISTS audit_events_admin_select ON audit_events")
    op.execute(
        "CREATE POLICY audit_events_admin_scope ON audit_events "
        "USING (lumenx_is_platform_admin()) WITH CHECK (lumenx_is_platform_admin())"
    )
