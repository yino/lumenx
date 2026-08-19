"""Physically separate administrator identities and privileged actors."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0017_physical_admin_identity"
down_revision: str | None = "0016_admin_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def upgrade() -> None:
    op.create_table(
        "admin_users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'suspended')", name="ck_admin_users_status"),
        sa.PrimaryKeyConstraint("id", name="pk_admin_users"),
        sa.UniqueConstraint("username", name="uq_admin_users_username"),
    )
    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("admin_user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("network_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.CheckConstraint(
            "idle_expires_at <= absolute_expires_at",
            name="ck_admin_sessions_expiry_order",
        ),
        sa.CheckConstraint(
            "idle_timeout_seconds IS NULL OR idle_timeout_seconds > 0",
            name="ck_admin_sessions_idle_timeout_positive",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_admin_sessions_token_hash"),
    )
    op.create_index(
        "ix_admin_sessions_admin_created",
        "admin_sessions",
        ["admin_user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_admin_sessions_expiry",
        "admin_sessions",
        ["revoked_at", "idle_expires_at", "absolute_expires_at"],
    )

    op.execute(
        """
        CREATE FUNCTION lumenx_current_admin_id() RETURNS bigint
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT CASE
                WHEN current_setting('app.current_admin_id', true) ~ '^[0-9]+$'
                THEN current_setting('app.current_admin_id', true)::bigint
                ELSE NULL
            END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_admin_login_username() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.admin_login_username', true), '')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_admin_session_token_hash() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.admin_session_token_hash', true), '')
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_has_admin_context() RETURNS boolean
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT lumenx_current_admin_id() IS NOT NULL $$
        """
    )
    # Existing policies retain this function name, but its authority now comes
    # exclusively from the independent administrator/system context.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION lumenx_is_platform_admin() RETURNS boolean
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT lumenx_has_admin_context() $$
        """
    )

    _enable_rls("admin_users")
    op.execute(
        """
        CREATE POLICY admin_users_select_login ON admin_users FOR SELECT
        USING (
            username = lumenx_admin_login_username()
            OR id = lumenx_current_admin_id()
            OR lumenx_current_admin_id() = 0
        )
        """
    )
    op.execute(
        """
        CREATE POLICY admin_users_insert_bootstrap ON admin_users FOR INSERT
        WITH CHECK (lumenx_current_admin_id() = 0)
        """
    )
    op.execute(
        """
        CREATE POLICY admin_users_update_self ON admin_users FOR UPDATE
        USING (id = lumenx_current_admin_id())
        WITH CHECK (id = lumenx_current_admin_id())
        """
    )
    _enable_rls("admin_sessions")
    op.execute(
        """
        CREATE POLICY admin_sessions_select_scope ON admin_sessions FOR SELECT
        USING (
            token_hash = lumenx_admin_session_token_hash()
            OR admin_user_id = lumenx_current_admin_id()
            OR lumenx_current_admin_id() = 0
        )
        """
    )
    op.execute(
        """
        CREATE POLICY admin_sessions_insert_scope ON admin_sessions FOR INSERT
        WITH CHECK (admin_user_id = lumenx_current_admin_id())
        """
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

    op.add_column("ticket_ledger", sa.Column("actor_admin_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_ticket_ledger_actor_admin", "ticket_ledger", ["actor_admin_id", "created_at", "id"])

    for column in (
        "created_by_admin_id",
        "completed_by_admin_id",
        "cancelled_by_admin_id",
        "last_refunded_by_admin_id",
    ):
        op.add_column("manual_recharge_orders", sa.Column(column, sa.BigInteger(), nullable=True))
    op.alter_column("manual_recharge_orders", "created_by_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.drop_constraint(
        "uq_manual_recharge_orders_create_idempotency",
        "manual_recharge_orders",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_manual_recharge_orders_create_idempotency",
        "manual_recharge_orders",
        ["created_by_admin_id", "create_idempotency_key"],
    )
    op.create_check_constraint(
        "ck_manual_recharge_orders_created_actor",
        "manual_recharge_orders",
        "(created_by_user_id IS NULL) <> (created_by_admin_id IS NULL)",
    )
    op.create_index(
        "ix_manual_recharge_orders_admin_created",
        "manual_recharge_orders",
        ["created_by_admin_id", "created_at", "id"],
    )

    op.add_column("manual_recharge_order_events", sa.Column("actor_admin_id", sa.BigInteger(), nullable=True))
    op.alter_column("manual_recharge_order_events", "actor_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.drop_constraint(
        "uq_manual_recharge_events_idempotency",
        "manual_recharge_order_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_manual_recharge_events_idempotency",
        "manual_recharge_order_events",
        ["actor_admin_id", "event_type", "idempotency_key"],
    )
    op.create_check_constraint(
        "ck_manual_recharge_events_actor",
        "manual_recharge_order_events",
        "(actor_user_id IS NULL) <> (actor_admin_id IS NULL)",
    )
    op.create_index(
        "ix_manual_recharge_events_admin_created",
        "manual_recharge_order_events",
        ["actor_admin_id", "created_at", "id"],
    )

    op.add_column(
        "manual_recharge_reconciliation_reports",
        sa.Column("actor_admin_id", sa.BigInteger(), nullable=True),
    )
    op.alter_column(
        "manual_recharge_reconciliation_reports",
        "actor_user_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_manual_recharge_reports_actor",
        "manual_recharge_reconciliation_reports",
        "(actor_user_id IS NULL) <> (actor_admin_id IS NULL)",
    )

    op.add_column("config_versions", sa.Column("created_by_admin_id", sa.BigInteger(), nullable=True))
    op.alter_column("config_versions", "created_by_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        "ck_config_versions_created_actor",
        "config_versions",
        "(created_by_user_id IS NULL) <> (created_by_admin_id IS NULL)",
    )
    op.create_index(
        "ix_config_versions_admin_created",
        "config_versions",
        ["created_by_admin_id", "created_at", "id"],
    )

    op.add_column("audit_events", sa.Column("actor_admin_id", sa.BigInteger(), nullable=True))
    op.create_index(
        "ix_audit_events_admin_created",
        "audit_events",
        ["actor_admin_id", "created_at", "id"],
    )
    op.execute('DROP POLICY IF EXISTS "audit_events_admin_select" ON "audit_events"')
    op.execute('DROP POLICY IF EXISTS "audit_events_scoped_insert" ON "audit_events"')
    op.execute(
        """
        CREATE POLICY audit_events_admin_select ON audit_events FOR SELECT
        USING (lumenx_has_admin_context())
        """
    )
    op.execute(
        """
        CREATE POLICY audit_events_scoped_insert ON audit_events FOR INSERT
        WITH CHECK (
            (actor_user_id = lumenx_current_user_id() AND actor_admin_id IS NULL)
            OR (
                actor_user_id IS NULL
                AND actor_admin_id IS NOT NULL
                AND lumenx_has_admin_context()
            )
        )
        """
    )

    op.add_column("import_batches", sa.Column("actor_admin_id", sa.BigInteger(), nullable=True))
    op.alter_column("import_batches", "actor_admin_user_id", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        "ck_import_batches_admin_actor",
        "import_batches",
        "(actor_admin_user_id IS NULL) <> (actor_admin_id IS NULL)",
    )
    op.create_index(
        "ix_import_batches_admin_created",
        "import_batches",
        ["actor_admin_id", "created_at", "id"],
    )

    for table in ("password_reset_credentials", "registration_invitations"):
        op.alter_column(
            table,
            "created_by_admin_id",
            new_column_name="legacy_created_by_user_id",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
        op.add_column(table, sa.Column("created_by_admin_id", sa.BigInteger(), nullable=True))
        op.alter_column(
            table,
            "legacy_created_by_user_id",
            existing_type=sa.BigInteger(),
            nullable=True,
        )
        op.create_check_constraint(
            f"ck_{table}_created_actor",
            table,
            "(legacy_created_by_user_id IS NULL) <> (created_by_admin_id IS NULL)",
        )

    op.execute(
        """
        UPDATE auth_sessions
        SET revoked_at = COALESCE(revoked_at, now())
        WHERE user_id IN (SELECT id FROM users WHERE is_platform_admin)
        """
    )
    op.drop_column("users", "is_platform_admin")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM admin_users LIMIT 1)
               OR EXISTS (SELECT 1 FROM audit_events WHERE actor_admin_id IS NOT NULL LIMIT 1)
               OR EXISTS (SELECT 1 FROM manual_recharge_orders WHERE created_by_admin_id IS NOT NULL LIMIT 1)
            THEN
                RAISE EXCEPTION 'cannot merge independent administrator history into users; apply a forward fix';
            END IF;
        END;
        $$
        """
    )
    op.add_column(
        "users",
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    for table in ("registration_invitations", "password_reset_credentials"):
        op.drop_constraint(f"ck_{table}_created_actor", table, type_="check")
        op.drop_column(table, "created_by_admin_id")
        op.alter_column(
            table,
            "legacy_created_by_user_id",
            new_column_name="created_by_admin_id",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
    op.drop_index("ix_import_batches_admin_created", table_name="import_batches")
    op.drop_constraint("ck_import_batches_admin_actor", "import_batches", type_="check")
    op.alter_column("import_batches", "actor_admin_user_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("import_batches", "actor_admin_id")
    op.execute('DROP POLICY IF EXISTS "audit_events_scoped_insert" ON "audit_events"')
    op.execute('DROP POLICY IF EXISTS "audit_events_admin_select" ON "audit_events"')
    op.execute(
        "CREATE POLICY audit_events_admin_select ON audit_events FOR SELECT "
        "USING (lumenx_is_platform_admin())"
    )
    op.execute(
        "CREATE POLICY audit_events_scoped_insert ON audit_events FOR INSERT "
        "WITH CHECK (actor_user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())"
    )
    op.drop_index("ix_audit_events_admin_created", table_name="audit_events")
    op.drop_column("audit_events", "actor_admin_id")
    op.drop_index("ix_config_versions_admin_created", table_name="config_versions")
    op.drop_constraint("ck_config_versions_created_actor", "config_versions", type_="check")
    op.alter_column("config_versions", "created_by_user_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("config_versions", "created_by_admin_id")
    op.drop_constraint("ck_manual_recharge_reports_actor", "manual_recharge_reconciliation_reports", type_="check")
    op.alter_column("manual_recharge_reconciliation_reports", "actor_user_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("manual_recharge_reconciliation_reports", "actor_admin_id")
    op.drop_index("ix_manual_recharge_events_admin_created", table_name="manual_recharge_order_events")
    op.drop_constraint("ck_manual_recharge_events_actor", "manual_recharge_order_events", type_="check")
    op.drop_constraint("uq_manual_recharge_events_idempotency", "manual_recharge_order_events", type_="unique")
    op.create_unique_constraint(
        "uq_manual_recharge_events_idempotency",
        "manual_recharge_order_events",
        ["actor_user_id", "event_type", "idempotency_key"],
    )
    op.alter_column("manual_recharge_order_events", "actor_user_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("manual_recharge_order_events", "actor_admin_id")
    op.drop_index("ix_manual_recharge_orders_admin_created", table_name="manual_recharge_orders")
    op.drop_constraint("ck_manual_recharge_orders_created_actor", "manual_recharge_orders", type_="check")
    op.drop_constraint("uq_manual_recharge_orders_create_idempotency", "manual_recharge_orders", type_="unique")
    op.create_unique_constraint(
        "uq_manual_recharge_orders_create_idempotency",
        "manual_recharge_orders",
        ["created_by_user_id", "create_idempotency_key"],
    )
    op.alter_column("manual_recharge_orders", "created_by_user_id", existing_type=sa.BigInteger(), nullable=False)
    for column in (
        "last_refunded_by_admin_id",
        "cancelled_by_admin_id",
        "completed_by_admin_id",
        "created_by_admin_id",
    ):
        op.drop_column("manual_recharge_orders", column)
    op.drop_index("ix_ticket_ledger_actor_admin", table_name="ticket_ledger")
    op.drop_column("ticket_ledger", "actor_admin_id")
    op.drop_table("admin_sessions")
    op.drop_table("admin_users")
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
    op.execute("DROP FUNCTION IF EXISTS lumenx_admin_session_token_hash()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_admin_login_username()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_current_admin_id()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_has_admin_context()")
