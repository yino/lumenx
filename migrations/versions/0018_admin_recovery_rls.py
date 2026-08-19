"""Allow explicit system recovery of the sole administrator identity."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0018_admin_recovery_rls"
down_revision: str | None = "0017_physical_admin_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "admin_users_update_self" ON "admin_users"')
    op.execute(
        """
        CREATE POLICY admin_users_update_self ON admin_users FOR UPDATE
        USING (
            id = lumenx_current_admin_id()
            OR lumenx_current_admin_id() = 0
        )
        WITH CHECK (
            id = lumenx_current_admin_id()
            OR lumenx_current_admin_id() = 0
        )
        """
    )
    op.execute(
        'DROP POLICY IF EXISTS "admin_sessions_update_scope" ON "admin_sessions"'
    )
    op.execute(
        """
        CREATE POLICY admin_sessions_update_scope ON admin_sessions FOR UPDATE
        USING (
            admin_user_id = lumenx_current_admin_id()
            OR token_hash = lumenx_admin_session_token_hash()
            OR lumenx_current_admin_id() = 0
        )
        WITH CHECK (
            admin_user_id = lumenx_current_admin_id()
            OR lumenx_current_admin_id() = 0
        )
        """
    )


def downgrade() -> None:
    op.execute(
        'DROP POLICY IF EXISTS "admin_sessions_update_scope" ON "admin_sessions"'
    )
    op.execute(
        """
        CREATE POLICY admin_sessions_update_scope ON admin_sessions FOR UPDATE
        USING (
            admin_user_id = lumenx_current_admin_id()
            OR token_hash = lumenx_admin_session_token_hash()
        )
        WITH CHECK (admin_user_id = lumenx_current_admin_id())
        """
    )
    op.execute('DROP POLICY IF EXISTS "admin_users_update_self" ON "admin_users"')
    op.execute(
        """
        CREATE POLICY admin_users_update_self ON admin_users FOR UPDATE
        USING (id = lumenx_current_admin_id())
        WITH CHECK (id = lumenx_current_admin_id())
        """
    )
