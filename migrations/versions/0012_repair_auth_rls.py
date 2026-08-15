"""Repair authentication RLS helpers and policies on upgraded databases."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0012_repair_auth_rls"
down_revision: str | None = "0011_safe_runtime_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_auth_context_functions() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lumenx_current_user_id() RETURNS bigint
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.current_user_id', true), '')::bigint
        $$
        """
    )
    for function_name, setting_name in (
        ("lumenx_reset_token_hash", "app.reset_token_hash"),
        ("lumenx_login_phone_canonical", "app.login_phone_canonical"),
        ("lumenx_registration_phone_canonical", "app.registration_phone_canonical"),
        ("lumenx_session_token_hash", "app.session_token_hash"),
        ("lumenx_registration_invitation_hash", "app.registration_invitation_hash"),
    ):
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {function_name}() RETURNS text
            LANGUAGE sql STABLE PARALLEL SAFE
            AS $$
                SELECT NULLIF(current_setting('{setting_name}', true), '')
            $$
            """
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lumenx_is_platform_admin() RETURNS boolean
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT COALESCE(
                NULLIF(current_setting('app.is_platform_admin', true), '')::boolean,
                false
            )
        $$
        """
    )


def _replace_policy(table: str, policy: str, definition: str) -> None:
    op.execute(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"')
    op.execute(f'CREATE POLICY "{policy}" ON "{table}" {definition}')


def upgrade() -> None:
    _replace_auth_context_functions()

    for table in (
        "users",
        "auth_sessions",
        "password_reset_credentials",
        "registration_invitations",
    ):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')

    _replace_policy(
        "users",
        "users_select_scope",
        """FOR SELECT USING (
            id = lumenx_current_user_id()
            OR phone_canonical = lumenx_login_phone_canonical()
            OR lumenx_is_platform_admin()
        )""",
    )
    _replace_policy(
        "users",
        "users_insert_scope",
        """FOR INSERT WITH CHECK (
            id = lumenx_current_user_id()
            OR phone_canonical = lumenx_registration_phone_canonical()
            OR lumenx_is_platform_admin()
        )""",
    )
    _replace_policy(
        "users",
        "users_update_scope",
        """FOR UPDATE
        USING (id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        WITH CHECK (id = lumenx_current_user_id() OR lumenx_is_platform_admin())""",
    )
    _replace_policy(
        "users",
        "users_delete_scope",
        "FOR DELETE USING (id = lumenx_current_user_id() OR lumenx_is_platform_admin())",
    )

    _replace_policy(
        "auth_sessions",
        "auth_sessions_select_scope",
        """FOR SELECT USING (
            user_id = lumenx_current_user_id()
            OR token_hash = lumenx_session_token_hash()
            OR lumenx_is_platform_admin()
        )""",
    )
    _replace_policy(
        "auth_sessions",
        "auth_sessions_insert_scope",
        """FOR INSERT WITH CHECK (
            user_id = lumenx_current_user_id() OR lumenx_is_platform_admin()
        )""",
    )
    _replace_policy(
        "auth_sessions",
        "auth_sessions_update_scope",
        """FOR UPDATE
        USING (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        WITH CHECK (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())""",
    )
    _replace_policy(
        "auth_sessions",
        "auth_sessions_delete_scope",
        "FOR DELETE USING (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())",
    )

    _replace_policy(
        "password_reset_credentials",
        "password_resets_select_scope",
        """FOR SELECT USING (
            token_hash = lumenx_reset_token_hash() OR lumenx_is_platform_admin()
        )""",
    )
    _replace_policy(
        "password_reset_credentials",
        "password_resets_insert_scope",
        "FOR INSERT WITH CHECK (lumenx_is_platform_admin())",
    )
    _replace_policy(
        "password_reset_credentials",
        "password_resets_update_scope",
        """FOR UPDATE
        USING (token_hash = lumenx_reset_token_hash() OR lumenx_is_platform_admin())
        WITH CHECK (token_hash = lumenx_reset_token_hash() OR lumenx_is_platform_admin())""",
    )

    _replace_policy(
        "registration_invitations",
        "registration_invitations_select_scope",
        """FOR SELECT USING (
            invitation_hash = lumenx_registration_invitation_hash()
            OR lumenx_is_platform_admin()
        )""",
    )
    _replace_policy(
        "registration_invitations",
        "registration_invitations_insert_admin",
        "FOR INSERT WITH CHECK (lumenx_is_platform_admin())",
    )
    _replace_policy(
        "registration_invitations",
        "registration_invitations_update_scope",
        """FOR UPDATE
        USING (
            invitation_hash = lumenx_registration_invitation_hash()
            OR lumenx_is_platform_admin()
        )
        WITH CHECK (
            invitation_hash = lumenx_registration_invitation_hash()
            OR lumenx_is_platform_admin()
        )""",
    )


def downgrade() -> None:
    # This migration repairs drift from the schema already declared by earlier
    # revisions. Keeping the repaired policies is safer than restoring drift.
    pass
