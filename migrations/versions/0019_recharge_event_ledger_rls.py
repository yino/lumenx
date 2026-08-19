"""Allow the guarded ledger link update on recharge events."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0019_recharge_event_rls"
down_revision: str | None = "0018_admin_recovery_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_POLICY_NAME = "manual_recharge_events_admin_link_ledger"


def upgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS "{_POLICY_NAME}" '
        'ON "manual_recharge_order_events"'
    )
    op.execute(
        f"""
        CREATE POLICY {_POLICY_NAME}
        ON manual_recharge_order_events
        FOR UPDATE
        USING (
            lumenx_is_platform_admin()
            AND ledger_entry_id IS NULL
        )
        WITH CHECK (
            lumenx_is_platform_admin()
            AND ledger_entry_id IS NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute(
        f'DROP POLICY IF EXISTS "{_POLICY_NAME}" '
        'ON "manual_recharge_order_events"'
    )
