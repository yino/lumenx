"""Allow registration inserts to return their generated user identifier."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0013_registration_returning_rls"
down_revision: str | None = "0012_repair_auth_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "users_select_scope" ON "users"')
    op.execute(
        """
        CREATE POLICY users_select_scope ON users FOR SELECT
        USING (
            id = lumenx_current_user_id()
            OR phone_canonical = lumenx_login_phone_canonical()
            OR phone_canonical = lumenx_registration_phone_canonical()
            OR lumenx_is_platform_admin()
        )
        """
    )


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "users_select_scope" ON "users"')
    op.execute(
        """
        CREATE POLICY users_select_scope ON users FOR SELECT
        USING (
            id = lumenx_current_user_id()
            OR phone_canonical = lumenx_login_phone_canonical()
            OR lumenx_is_platform_admin()
        )
        """
    )
