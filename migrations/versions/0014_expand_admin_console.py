"""Add administrator username, recharge orders, and system media scope."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0014_expand_admin_console"
down_revision: str | None = "0013_registration_returning_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enable_rls(table: str) -> None:
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(length=32), nullable=True))
    op.alter_column("users", "phone_canonical", existing_type=sa.String(length=32), nullable=True)
    op.create_unique_constraint("uq_users_username", "users", ["username"])
    op.create_check_constraint(
        "ck_users_login_identity",
        "users",
        "phone_canonical IS NOT NULL OR username IS NOT NULL",
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_login_username() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.login_username', true), '')
        $$
        """
    )
    op.execute('DROP POLICY IF EXISTS "users_select_scope" ON "users"')
    op.execute(
        """
        CREATE POLICY users_select_scope ON users FOR SELECT
        USING (
            id = lumenx_current_user_id()
            OR phone_canonical = lumenx_login_phone_canonical()
            OR username = lumenx_login_username()
            OR phone_canonical = lumenx_registration_phone_canonical()
            OR lumenx_is_platform_admin()
        )
        """
    )

    op.add_column(
        "media_objects",
        sa.Column("scope", sa.String(length=24), nullable=False, server_default="user"),
    )
    op.alter_column("media_objects", "user_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column("media_objects", "workspace_id", existing_type=sa.BigInteger(), nullable=True)
    op.drop_constraint("ck_media_objects_lifecycle", "media_objects", type_="check")
    op.create_check_constraint(
        "ck_media_objects_lifecycle",
        "media_objects",
        "lifecycle_state IN ('pending', 'active', 'quarantined', 'deleted', 'failed')",
    )
    op.create_check_constraint(
        "ck_media_objects_scope",
        "media_objects",
        "scope IN ('user', 'system')",
    )
    op.create_check_constraint(
        "ck_media_objects_scope_owner",
        "media_objects",
        "(scope = 'system' AND user_id IS NULL AND workspace_id IS NULL "
        "AND project_id IS NULL) OR "
        "(scope = 'user' AND user_id IS NOT NULL AND workspace_id IS NOT NULL)",
    )
    op.create_index(
        "ix_media_objects_scope_lifecycle_created",
        "media_objects",
        ["scope", "lifecycle_state", "created_at", "id"],
    )
    op.execute('DROP POLICY IF EXISTS "media_objects_user_scope" ON "media_objects"')
    op.execute(
        """
        CREATE POLICY media_objects_user_scope ON media_objects
        USING (
            (scope = 'user' AND user_id = lumenx_current_user_id())
            OR lumenx_is_platform_admin()
            OR (
                scope = 'system' AND lifecycle_state = 'active' AND deleted_at IS NULL
                AND EXISTS (
                    SELECT 1 FROM assets
                    WHERE assets.scope = 'system'
                      AND assets.asset_type = 'scene'
                      AND assets.media_object_id = media_objects.id
                      AND assets.deleted_at IS NULL
                      AND COALESCE(assets.payload->>'visibility', 'disabled') = 'enabled'
                )
            )
        )
        WITH CHECK (
            (scope = 'user' AND user_id = lumenx_current_user_id())
            OR lumenx_is_platform_admin()
        )
        """
    )

    op.add_column(
        "ticket_wallets",
        sa.Column(
            "lifetime_recharged_microtickets",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "ticket_wallets",
        sa.Column(
            "lifetime_refunded_microtickets",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.drop_constraint("ck_ticket_wallets_nonnegative", "ticket_wallets", type_="check")
    op.create_check_constraint(
        "ck_ticket_wallets_nonnegative",
        "ticket_wallets",
        "available_microtickets >= 0 AND held_microtickets >= 0 AND "
        "lifetime_granted_microtickets >= 0 AND lifetime_spent_microtickets >= 0 AND "
        "lifetime_recharged_microtickets >= 0 AND lifetime_refunded_microtickets >= 0",
    )

    op.add_column(
        "ticket_ledger",
        sa.Column("manual_recharge_order_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "ticket_ledger",
        sa.Column("manual_recharge_event_id", sa.BigInteger(), nullable=True),
    )
    op.drop_constraint("ck_ticket_ledger_entry_type", "ticket_ledger", type_="check")
    op.create_check_constraint(
        "ck_ticket_ledger_entry_type",
        "ticket_ledger",
        "entry_type IN ('grant', 'hold', 'settlement', 'release', 'adjustment', "
        "'compensation', 'manual_recharge', 'manual_recharge_refund')",
    )
    op.create_index(
        "ix_ticket_ledger_recharge_order",
        "ticket_ledger",
        ["manual_recharge_order_id", "created_at", "id"],
    )
    op.create_index(
        "ix_ticket_ledger_recharge_event",
        "ticket_ledger",
        ["manual_recharge_event_id"],
    )

    op.create_table(
        "manual_recharge_orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("order_number", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("cash_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("ticket_amount_microtickets", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="CNY"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("exchange_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("offline_reference", sa.String(length=160), nullable=True),
        sa.Column("create_reason", sa.Text(), nullable=False),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("refunded_cash_fen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("refunded_microtickets", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("create_idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("create_request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("completed_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("cancelled_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("last_refunded_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'cancelled', 'partially_refunded', 'refunded')",
            name="ck_manual_recharge_orders_status",
        ),
        sa.CheckConstraint("currency = 'CNY'", name="ck_manual_recharge_orders_currency"),
        sa.CheckConstraint("cash_amount_fen > 0", name="ck_manual_recharge_orders_cash_positive"),
        sa.CheckConstraint(
            "ticket_amount_microtickets > 0",
            name="ck_manual_recharge_orders_tickets_positive",
        ),
        sa.CheckConstraint(
            "refunded_cash_fen >= 0 AND refunded_cash_fen <= cash_amount_fen",
            name="ck_manual_recharge_orders_refunded_cash",
        ),
        sa.CheckConstraint(
            "refunded_microtickets >= 0 AND refunded_microtickets <= ticket_amount_microtickets",
            name="ck_manual_recharge_orders_refunded_tickets",
        ),
        sa.CheckConstraint("version > 0", name="ck_manual_recharge_orders_version_positive"),
        sa.UniqueConstraint("order_number", name="uq_manual_recharge_orders_number"),
        sa.UniqueConstraint(
            "created_by_user_id",
            "create_idempotency_key",
            name="uq_manual_recharge_orders_create_idempotency",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_manual_recharge_orders"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_manual_recharge_orders_offline_reference "
        "ON manual_recharge_orders (lower(offline_reference)) "
        "WHERE offline_reference IS NOT NULL"
    )
    op.create_index(
        "ix_manual_recharge_orders_user_status_created",
        "manual_recharge_orders",
        ["user_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_manual_recharge_orders_status_updated",
        "manual_recharge_orders",
        ["status", "updated_at", "id"],
    )
    op.create_index(
        "ix_manual_recharge_orders_actor_created",
        "manual_recharge_orders",
        ["created_by_user_id", "created_at", "id"],
    )

    op.create_table(
        "manual_recharge_order_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("cash_amount_fen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ticket_amount_microtickets", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ledger_entry_id", sa.BigInteger(), nullable=True),
        sa.Column("version_before", sa.Integer(), nullable=False),
        sa.Column("version_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "event_type IN ('created', 'completed', 'cancelled', 'refunded')",
            name="ck_manual_recharge_order_events_type",
        ),
        sa.CheckConstraint("cash_amount_fen >= 0", name="ck_manual_recharge_events_cash"),
        sa.CheckConstraint(
            "ticket_amount_microtickets >= 0",
            name="ck_manual_recharge_events_tickets",
        ),
        sa.CheckConstraint(
            "version_before >= 0 AND version_after > version_before",
            name="ck_manual_recharge_events_versions",
        ),
        sa.UniqueConstraint(
            "actor_user_id",
            "event_type",
            "idempotency_key",
            name="uq_manual_recharge_events_idempotency",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_manual_recharge_order_events"),
    )
    op.create_index(
        "ix_manual_recharge_events_order_created",
        "manual_recharge_order_events",
        ["order_id", "created_at", "id"],
    )
    op.create_index(
        "ix_manual_recharge_events_user_created",
        "manual_recharge_order_events",
        ["user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_manual_recharge_events_ledger",
        "manual_recharge_order_events",
        ["ledger_entry_id"],
    )

    op.create_table(
        "manual_recharge_reconciliation_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("severity", sa.String(length=24), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("correlation_id", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('consistent', 'mismatch')",
            name="ck_manual_recharge_reconciliation_status",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'high')",
            name="ck_manual_recharge_reconciliation_severity",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_manual_recharge_reconciliation_reports"),
    )
    op.create_index(
        "ix_manual_recharge_reports_status_created",
        "manual_recharge_reconciliation_reports",
        ["status", "severity", "created_at", "id"],
    )
    op.create_index(
        "ix_manual_recharge_reports_order",
        "manual_recharge_reconciliation_reports",
        ["order_id", "created_at", "id"],
    )

    op.execute(
        """
        CREATE FUNCTION lumenx_reject_financial_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'financial history is immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_protect_recharge_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE'
               AND OLD.ledger_entry_id IS NULL
               AND NEW.ledger_entry_id IS NOT NULL
               AND (to_jsonb(NEW) - 'ledger_entry_id') =
                   (to_jsonb(OLD) - 'ledger_entry_id') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'recharge order events are immutable';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER ticket_ledger_immutable BEFORE UPDATE OR DELETE ON ticket_ledger "
        "FOR EACH ROW EXECUTE FUNCTION lumenx_reject_financial_history_mutation()"
    )
    op.execute(
        "CREATE TRIGGER manual_recharge_order_events_immutable BEFORE UPDATE OR DELETE "
        "ON manual_recharge_order_events FOR EACH ROW "
        "EXECUTE FUNCTION lumenx_protect_recharge_event()"
    )

    _enable_rls("manual_recharge_orders")
    op.execute(
        """
        CREATE POLICY manual_recharge_orders_select_scope ON manual_recharge_orders
        FOR SELECT USING (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY manual_recharge_orders_admin_insert ON manual_recharge_orders
        FOR INSERT WITH CHECK (lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY manual_recharge_orders_admin_update ON manual_recharge_orders
        FOR UPDATE USING (lumenx_is_platform_admin()) WITH CHECK (lumenx_is_platform_admin())
        """
    )
    _enable_rls("manual_recharge_order_events")
    op.execute(
        """
        CREATE POLICY manual_recharge_events_select_scope ON manual_recharge_order_events
        FOR SELECT USING (user_id = lumenx_current_user_id() OR lumenx_is_platform_admin())
        """
    )
    op.execute(
        """
        CREATE POLICY manual_recharge_events_admin_insert ON manual_recharge_order_events
        FOR INSERT WITH CHECK (lumenx_is_platform_admin())
        """
    )
    _enable_rls("manual_recharge_reconciliation_reports")
    op.execute(
        """
        CREATE POLICY manual_recharge_reports_admin_scope
        ON manual_recharge_reconciliation_reports
        USING (lumenx_is_platform_admin()) WITH CHECK (lumenx_is_platform_admin())
        """
    )

    op.execute(
        "CREATE INDEX ix_assets_system_scene_catalog ON assets "
        "(asset_type, (payload->>'visibility'), (payload->>'category'), "
        "((CASE WHEN (payload->>'sort_order') ~ '^[0-9]+$' "
        "THEN (payload->>'sort_order')::integer ELSE 100 END)), id) "
        "WHERE scope = 'system' AND asset_type = 'scene' AND deleted_at IS NULL"
    )
    op.create_index(
        "ix_audit_events_target_action_created",
        "audit_events",
        ["target_user_id", "workspace_id", "action", "created_at", "id"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM manual_recharge_orders LIMIT 1) THEN
                RAISE EXCEPTION 'cannot downgrade after financial events; apply a forward fix';
            END IF;
        END;
        $$
        """
    )
    op.drop_index("ix_audit_events_target_action_created", table_name="audit_events")
    op.drop_index("ix_assets_system_scene_catalog", table_name="assets")
    for table in ("manual_recharge_order_events", "ticket_ledger"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS lumenx_protect_recharge_event()")
    op.execute("DROP FUNCTION IF EXISTS lumenx_reject_financial_history_mutation()")
    op.drop_table("manual_recharge_reconciliation_reports")
    op.drop_table("manual_recharge_order_events")
    op.drop_table("manual_recharge_orders")
    op.drop_index("ix_ticket_ledger_recharge_event", table_name="ticket_ledger")
    op.drop_index("ix_ticket_ledger_recharge_order", table_name="ticket_ledger")
    op.drop_column("ticket_ledger", "manual_recharge_event_id")
    op.drop_column("ticket_ledger", "manual_recharge_order_id")
    op.drop_column("ticket_wallets", "lifetime_refunded_microtickets")
    op.drop_column("ticket_wallets", "lifetime_recharged_microtickets")
    op.drop_index("ix_media_objects_scope_lifecycle_created", table_name="media_objects")
    op.drop_constraint("ck_media_objects_scope_owner", "media_objects", type_="check")
    op.drop_constraint("ck_media_objects_scope", "media_objects", type_="check")
    op.drop_column("media_objects", "scope")
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
    op.execute("DROP FUNCTION IF EXISTS lumenx_login_username()")
    op.drop_constraint("ck_users_login_identity", "users", type_="check")
    op.drop_constraint("uq_users_username", "users", type_="unique")
    op.drop_column("users", "username")
    op.alter_column("users", "phone_canonical", existing_type=sa.String(length=32), nullable=False)
