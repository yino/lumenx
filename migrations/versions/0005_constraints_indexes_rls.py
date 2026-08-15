"""Add cloud indexes, immutability guards, and row-level security."""

from collections.abc import Sequence

from alembic import op


revision: str = "0005_constraints_indexes_rls"
down_revision: str | None = "0004_admin_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


USER_SCOPED_TABLES = {
    "workspaces": "user_id",
    "series": "user_id",
    "projects": "user_id",
    "media_objects": "user_id",
    "ai_tasks": "user_id",
    "ai_task_attempts": "user_id",
    "ticket_wallets": "user_id",
    "ticket_holds": "user_id",
    "ticket_ledger": "user_id",
    "usage_events": "user_id",
}

ADMIN_ONLY_TABLES = (
    "config_versions",
    "model_configs",
    "platform_configs",
    "audit_events",
    "import_batches",
)


def _enable_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def upgrade() -> None:
    op.create_index(
        "ix_auth_sessions_user_active_expiry",
        "auth_sessions",
        ["user_id", "revoked_at", "idle_expires_at", "absolute_expires_at"],
    )
    op.create_index(
        "ix_workspaces_user_deleted",
        "workspaces",
        ["user_id", "deleted_at", "updated_at"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_workspaces_active_name ON workspaces "
        "(user_id, lower(name)) WHERE deleted_at IS NULL"
    )
    for table in ("series", "projects"):
        op.create_index(
            f"ix_{table}_owner_active_updated",
            table,
            ["user_id", "workspace_id", "deleted_at", "updated_at"],
        )
    op.create_index(
        "ix_assets_resolver",
        "assets",
        ["user_id", "workspace_id", "scope", "project_id", "series_id", "asset_type", "deleted_at"],
    )
    op.create_index(
        "ix_media_objects_owner_lifecycle",
        "media_objects",
        ["user_id", "workspace_id", "project_id", "lifecycle_state", "deleted_at"],
    )
    op.create_unique_constraint(
        "uq_ai_tasks_user_idempotency_key",
        "ai_tasks",
        ["user_id", "idempotency_key"],
    )
    op.create_index(
        "ix_ai_tasks_owner_status_created",
        "ai_tasks",
        ["user_id", "workspace_id", "status", "created_at"],
    )
    op.create_index("ix_ai_tasks_project_created", "ai_tasks", ["project_id", "created_at"])
    op.create_index(
        "ix_ai_task_attempts_task_status",
        "ai_task_attempts",
        ["task_id", "status", "attempt_number"],
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ai_task_attempts_provider_task ON ai_task_attempts "
        "(provider, provider_task_id) WHERE provider_task_id IS NOT NULL"
    )
    op.create_index(
        "ix_ticket_holds_user_status_created",
        "ticket_holds",
        ["user_id", "status", "created_at"],
    )
    op.create_index(
        "ix_ticket_ledger_user_created",
        "ticket_ledger",
        ["user_id", "created_at", "id"],
    )
    op.create_index("ix_ticket_ledger_task", "ticket_ledger", ["task_id", "created_at"])
    op.create_index(
        "ix_usage_events_user_created",
        "usage_events",
        ["user_id", "workspace_id", "created_at", "id"],
    )
    op.create_index("ix_usage_events_task", "usage_events", ["task_id", "created_at"])
    op.execute(
        "CREATE UNIQUE INDEX uq_config_versions_single_active ON config_versions (status) "
        "WHERE status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_model_configs_enabled_primary ON model_configs "
        "(config_version_id, capability) WHERE enabled AND is_primary"
    )
    op.create_index(
        "ix_model_configs_route_selection",
        "model_configs",
        ["config_version_id", "capability", "enabled", "priority"],
    )
    op.create_index("ix_audit_events_created", "audit_events", ["created_at", "id"])
    op.create_index("ix_audit_events_correlation", "audit_events", ["correlation_id"])
    op.create_index(
        "ix_import_batches_status_created", "import_batches", ["status", "created_at"]
    )

    op.execute(
        """
        CREATE FUNCTION lumenx_current_user_id() RETURNS bigint
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.current_user_id', true), '')::bigint
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_reset_token_hash() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.reset_token_hash', true), '')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_is_platform_admin() RETURNS boolean
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT COALESCE(
                NULLIF(current_setting('app.is_platform_admin', true), '')::boolean,
                false
            )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_login_phone_canonical() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.login_phone_canonical', true), '')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_registration_phone_canonical() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.registration_phone_canonical', true), '')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_session_token_hash() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.session_token_hash', true), '')
        $$
        """
    )

    _enable_rls("users")
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
    op.execute(
        """
        CREATE POLICY users_insert_scope ON users FOR INSERT
        WITH CHECK (
            id = lumenx_current_user_id()
            OR phone_canonical = lumenx_registration_phone_canonical()
            OR lumenx_is_platform_admin()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY users_update_scope ON users FOR UPDATE
        USING (id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        WITH CHECK (id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY users_delete_scope ON users FOR DELETE
        USING (id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )

    _enable_rls("auth_sessions")
    op.execute(
        """
        CREATE POLICY auth_sessions_select_scope ON auth_sessions FOR SELECT
        USING (
            user_id = lumenx_current_user_id()
            OR token_hash = lumenx_session_token_hash()
            OR lumenx_is_platform_admin()
        )
        """
    )
    op.execute(
        """
        CREATE POLICY auth_sessions_insert_scope ON auth_sessions FOR INSERT
        WITH CHECK (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY auth_sessions_update_scope ON auth_sessions FOR UPDATE
        USING (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        WITH CHECK (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY auth_sessions_delete_scope ON auth_sessions FOR DELETE
        USING (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )

    _enable_rls("password_reset_credentials")
    op.execute(
        """
        CREATE POLICY password_resets_select_scope ON password_reset_credentials FOR SELECT
        USING (token_hash = lumenx_reset_token_hash() OR lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY password_resets_insert_scope ON password_reset_credentials FOR INSERT
        WITH CHECK (lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY password_resets_update_scope ON password_reset_credentials FOR UPDATE
        USING (token_hash = lumenx_reset_token_hash() OR lumenx_is_platform_admin())
        WITH CHECK (token_hash = lumenx_reset_token_hash() OR lumenx_is_platform_admin())
        """
    )

    for table, user_column in USER_SCOPED_TABLES.items():
        _enable_rls(table)
        op.execute(
            f'CREATE POLICY "{table}_user_scope" ON "{table}" '
            f"USING ({user_column} = lumenx_current_user_id() OR lumenx_is_platform_admin()) "
            f"WITH CHECK ({user_column} = lumenx_current_user_id() OR lumenx_is_platform_admin())"
        )

    _enable_rls("assets")
    op.execute(
        """
        CREATE POLICY assets_user_scope ON assets
        USING (
            scope = 'system' OR user_id = lumenx_current_user_id() OR lumenx_is_platform_admin()
        )
        WITH CHECK (
            (scope <> 'system' AND user_id = lumenx_current_user_id())
            OR lumenx_is_platform_admin()
        )
        """
    )

    for table in ADMIN_ONLY_TABLES:
        _enable_rls(table)
        op.execute(
            f'CREATE POLICY "{table}_admin_scope" ON "{table}" '
            "USING (lumenx_is_platform_admin()) WITH CHECK (lumenx_is_platform_admin())"
        )

    op.execute(
        """
        CREATE FUNCTION reject_ticket_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'ticket_ledger is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER ticket_ledger_append_only
        BEFORE UPDATE OR DELETE ON ticket_ledger
        FOR EACH ROW EXECUTE FUNCTION reject_ticket_ledger_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ticket_ledger_append_only ON ticket_ledger")
    op.execute("DROP FUNCTION IF EXISTS reject_ticket_ledger_mutation()")

    for table in ADMIN_ONLY_TABLES:
        op.execute(f'DROP POLICY IF EXISTS "{table}_admin_scope" ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    op.execute("DROP POLICY IF EXISTS assets_user_scope ON assets")
    op.execute("ALTER TABLE assets DISABLE ROW LEVEL SECURITY")
    for table in reversed(tuple(USER_SCOPED_TABLES)):
        op.execute(f'DROP POLICY IF EXISTS "{table}_user_scope" ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    for policy in (
        "password_resets_update_scope",
        "password_resets_insert_scope",
        "password_resets_select_scope",
    ):
        op.execute(f'DROP POLICY IF EXISTS "{policy}" ON password_reset_credentials')
    op.execute("ALTER TABLE password_reset_credentials DISABLE ROW LEVEL SECURITY")

    for policy in (
        "auth_sessions_delete_scope",
        "auth_sessions_update_scope",
        "auth_sessions_insert_scope",
        "auth_sessions_select_scope",
    ):
        op.execute(f'DROP POLICY IF EXISTS "{policy}" ON auth_sessions')
    op.execute("ALTER TABLE auth_sessions DISABLE ROW LEVEL SECURITY")
    for policy in (
        "users_delete_scope",
        "users_update_scope",
        "users_insert_scope",
        "users_select_scope",
    ):
        op.execute(f'DROP POLICY IF EXISTS "{policy}" ON users')
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS lumenx_reset_token_hash()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_session_token_hash()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_registration_phone_canonical()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_login_phone_canonical()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_is_platform_admin()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_current_user_id()")

    op.drop_index("ix_import_batches_status_created", table_name="import_batches")
    op.drop_index("ix_audit_events_correlation", table_name="audit_events")
    op.drop_index("ix_audit_events_created", table_name="audit_events")
    op.drop_index("ix_model_configs_route_selection", table_name="model_configs")
    op.execute("DROP INDEX IF EXISTS uq_model_configs_enabled_primary")
    op.execute("DROP INDEX IF EXISTS uq_config_versions_single_active")
    op.drop_index("ix_usage_events_task", table_name="usage_events")
    op.drop_index("ix_usage_events_user_created", table_name="usage_events")
    op.drop_index("ix_ticket_ledger_task", table_name="ticket_ledger")
    op.drop_index("ix_ticket_ledger_user_created", table_name="ticket_ledger")
    op.drop_index("ix_ticket_holds_user_status_created", table_name="ticket_holds")
    op.execute("DROP INDEX IF EXISTS uq_ai_task_attempts_provider_task")
    op.drop_index("ix_ai_task_attempts_task_status", table_name="ai_task_attempts")
    op.drop_index("ix_ai_tasks_project_created", table_name="ai_tasks")
    op.drop_index("ix_ai_tasks_owner_status_created", table_name="ai_tasks")
    op.drop_constraint("uq_ai_tasks_user_idempotency_key", "ai_tasks", type_="unique")
    op.drop_index("ix_media_objects_owner_lifecycle", table_name="media_objects")
    op.drop_index("ix_assets_resolver", table_name="assets")
    op.drop_index("ix_projects_owner_active_updated", table_name="projects")
    op.drop_index("ix_series_owner_active_updated", table_name="series")
    op.execute("DROP INDEX IF EXISTS uq_workspaces_active_name")
    op.drop_index("ix_workspaces_user_deleted", table_name="workspaces")
    op.drop_index("ix_auth_sessions_user_active_expiry", table_name="auth_sessions")
