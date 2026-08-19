#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.platform.contracts import AdminContext
from src.platform.database import Database
from src.platform.db_models import (
    ManualRechargeOrderEventRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
)
from src.platform.manual_recharge import ManualRechargeOrderService


DATABASE_NAMES = ("lumenx_release_fresh", "lumenx_release_legacy")
RLS_TABLES = (
    "users",
    "workspaces",
    "assets",
    "media_objects",
    "ai_tasks",
    "ticket_wallets",
    "ticket_ledger",
    "manual_recharge_orders",
)


def _database_url(base_url: URL, database_name: str) -> str:
    return base_url.set(database=database_name).render_as_string(
        hide_password=False
    )


def _run_alembic(database_url: str, target: str) -> None:
    environment = os.environ.copy()
    environment["LUMENX_DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        check=True,
        env=environment,
    )


def _assert_database_identifier_policy(connection) -> None:
    invalid_primary_keys = connection.execute(
        text(
            """
            WITH app_tables AS (
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name <> 'alembic_version'
            ), primary_keys AS (
                SELECT tc.table_name,
                       array_agg(kcu.column_name::text ORDER BY kcu.ordinal_position) AS columns
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = 'public'
                  AND tc.constraint_type = 'PRIMARY KEY'
                GROUP BY tc.table_name
            )
            SELECT app_tables.table_name, primary_keys.columns
            FROM app_tables
            LEFT JOIN primary_keys USING (table_name)
            WHERE primary_keys.columns IS DISTINCT FROM ARRAY['id']::text[]
            ORDER BY app_tables.table_name
            """
        )
    ).all()
    invalid_id_columns = connection.execute(
        text(
            """
            SELECT tables.table_name, columns.data_type, columns.is_identity
            FROM information_schema.tables AS tables
            LEFT JOIN information_schema.columns AS columns
              ON columns.table_schema = tables.table_schema
             AND columns.table_name = tables.table_name
             AND columns.column_name = 'id'
            WHERE tables.table_schema = 'public'
              AND tables.table_type = 'BASE TABLE'
              AND tables.table_name <> 'alembic_version'
              AND (
                  columns.column_name IS NULL
                  OR columns.data_type <> 'bigint'
                  OR columns.is_identity <> 'YES'
              )
            ORDER BY tables.table_name
            """
        )
    ).all()
    uuid_columns = connection.execute(
        text(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND data_type = 'uuid'
            ORDER BY table_name, ordinal_position
            """
        )
    ).all()
    foreign_keys = connection.execute(
        text(
            """
            SELECT relation.relname, constraint_record.conname
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public' AND constraint_record.contype = 'f'
            ORDER BY relation.relname, constraint_record.conname
            """
        )
    ).all()
    violations = {
        "invalid_primary_keys": [tuple(row) for row in invalid_primary_keys],
        "invalid_id_columns": [tuple(row) for row in invalid_id_columns],
        "uuid_columns": [tuple(row) for row in uuid_columns],
        "foreign_keys": [tuple(row) for row in foreign_keys],
    }
    assert not any(violations.values()), json.dumps(violations, ensure_ascii=False, default=str)


def _assert_auth_rls_contract(connection) -> None:
    expected_functions = {
        "lumenx_current_user_id",
        "lumenx_current_admin_id",
        "lumenx_has_admin_context",
        "lumenx_is_platform_admin",
        "lumenx_admin_login_username",
        "lumenx_admin_session_token_hash",
        "lumenx_login_phone_canonical",
        "lumenx_login_username",
        "lumenx_registration_phone_canonical",
        "lumenx_session_token_hash",
        "lumenx_reset_token_hash",
        "lumenx_registration_invitation_hash",
    }
    existing_functions = set(
        connection.scalars(
            text(
                "SELECT proname FROM pg_proc "
                "WHERE pronamespace = 'public'::regnamespace "
                "AND proname = ANY(:function_names)"
            ),
            {"function_names": sorted(expected_functions)},
        )
    )
    policies = {
        row.policyname: row
        for row in connection.execute(
            text(
                "SELECT policyname, qual, with_check FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = 'users'"
            )
        )
    }
    insert_check = str(policies["users_insert_scope"].with_check)
    select_check = str(policies["users_select_scope"].qual)

    assert existing_functions == expected_functions
    assert "lumenx_registration_phone_canonical" in insert_check
    assert "lumenx_login_phone_canonical" in select_check
    assert "lumenx_login_username" in select_check
    assert "lumenx_registration_phone_canonical" in select_check
    admin_compatibility_function = connection.scalar(
        text(
            "SELECT pg_get_functiondef('lumenx_is_platform_admin()'::regprocedure)"
        )
    )
    assert "lumenx_has_admin_context" in admin_compatibility_function
    assert "app.is_platform_admin" not in admin_compatibility_function


def _assert_admin_console_schema(connection) -> None:
    tables = set(
        connection.scalars(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        )
    )
    assert {
        "admin_users",
        "admin_sessions",
        "manual_recharge_orders",
        "manual_recharge_order_events",
        "manual_recharge_reconciliation_reports",
    } <= tables
    user_columns = {
        row.column_name: row
        for row in connection.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users'"
            )
        )
    }
    assert "username" in user_columns
    assert "is_platform_admin" not in user_columns
    assert user_columns["phone_canonical"].is_nullable == "YES"
    media_scopes = set(connection.scalars(text("SELECT DISTINCT scope FROM media_objects")))
    assert media_scopes <= {"user", "system"}
    triggers = set(
        connection.scalars(
            text(
                "SELECT trigger_name FROM information_schema.triggers "
                "WHERE event_object_schema = 'public'"
            )
        )
    )
    assert {
        "ticket_ledger_immutable",
        "manual_recharge_order_events_immutable",
    } <= triggers
    required_indexes = {
        "ix_users_admin_created",
        "ix_admin_sessions_admin_created",
        "ix_admin_sessions_expiry",
        "ix_audit_events_admin_created",
        "ix_ticket_ledger_actor_admin",
        "ix_ai_tasks_admin_created",
        "ix_ai_tasks_admin_owner_created",
        "ix_usage_events_admin_created",
        "ix_assets_admin_owner_updated",
        "ix_media_objects_admin_owner_created",
        "ix_manual_recharge_orders_status_created",
        "ix_manual_recharge_orders_user_status_created",
        "ix_manual_recharge_events_order_created",
        "ix_ticket_ledger_recharge_order",
        "ix_media_objects_scope_lifecycle_created",
        "ix_assets_system_scene_catalog",
    }
    indexes = set(
        connection.scalars(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        )
    )
    assert required_indexes <= indexes
    policies = {
        (row.tablename, row.policyname)
        for row in connection.execute(
            text(
                "SELECT tablename, policyname FROM pg_policies "
                "WHERE schemaname = 'public'"
            )
        )
    }
    assert {
        ("admin_users", "admin_users_select_login"),
        ("admin_sessions", "admin_sessions_select_scope"),
        ("manual_recharge_orders", "manual_recharge_orders_select_scope"),
        ("manual_recharge_order_events", "manual_recharge_events_select_scope"),
        (
            "manual_recharge_order_events",
            "manual_recharge_events_admin_link_ledger",
        ),
        (
            "manual_recharge_reconciliation_reports",
            "manual_recharge_reports_admin_scope",
        ),
    } <= policies
    admin_user_update_policy = connection.execute(
        text(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = 'admin_users' "
            "AND policyname = 'admin_users_update_self'"
        )
    ).one()
    admin_session_update_policy = connection.execute(
        text(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = 'admin_sessions' "
            "AND policyname = 'admin_sessions_update_scope'"
        )
    ).one()
    for policy in (admin_user_update_policy, admin_session_update_policy):
        assert "lumenx_current_admin_id() = 0" in str(policy.qual)
        assert "lumenx_current_admin_id() = 0" in str(policy.with_check)
    recharge_event_update_policy = connection.execute(
        text(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' "
            "AND tablename = 'manual_recharge_order_events' "
            "AND policyname = 'manual_recharge_events_admin_link_ledger'"
        )
    ).one()
    assert "lumenx_is_platform_admin()" in str(recharge_event_update_policy.qual)
    assert "ledger_entry_id IS NULL" in str(recharge_event_update_policy.qual)
    assert "lumenx_is_platform_admin()" in str(
        recharge_event_update_policy.with_check
    )
    assert "ledger_entry_id IS NOT NULL" in str(
        recharge_event_update_policy.with_check
    )
    media_policy = connection.execute(
        text(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = 'media_objects' "
            "AND policyname = 'media_objects_user_scope'"
        )
    ).one()
    media_qual = str(media_policy.qual)
    media_check = str(media_policy.with_check)
    assert "system_scene_copy" in media_qual
    assert "lumenx_current_user_id" in media_qual
    assert "lumenx_is_platform_admin" in media_qual
    assert "lumenx_is_platform_admin" in media_check
    assert "app.is_platform_admin" not in media_qual
    assert "app.is_platform_admin" not in media_check

    actor_checks = {
        row.constraint_name: row.check_clause
        for row in connection.execute(
            text(
                "SELECT constraint_name, check_clause "
                "FROM information_schema.check_constraints "
                "WHERE constraint_schema = 'public'"
            )
        )
    }
    for constraint_name in (
        "ck_admin_sessions_expiry_order",
        "ck_admin_sessions_idle_timeout_positive",
        "ck_manual_recharge_orders_created_actor",
        "ck_manual_recharge_events_actor",
        "ck_manual_recharge_reports_actor",
        "ck_config_versions_created_actor",
        "ck_import_batches_admin_actor",
        "ck_password_reset_credentials_created_actor",
        "ck_registration_invitations_created_actor",
    ):
        assert constraint_name in actor_checks


def _assert_admin_query_plans(
    connection,
    fixtures: dict[str, tuple[int, int]],
) -> None:
    first_user_id = fixtures["users"][0]
    first_workspace_id = fixtures["workspaces"][0]
    connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
    query_plans = {
        "ix_users_admin_created": """
            SELECT id FROM users
            WHERE created_at >= now() - interval '1 day'
            ORDER BY created_at DESC, id DESC LIMIT 30
        """,
        "ix_manual_recharge_orders_status_created": """
            SELECT id FROM manual_recharge_orders
            WHERE status = 'pending' AND created_at >= now() - interval '1 day'
            ORDER BY created_at DESC, id DESC LIMIT 30
        """,
        "ix_ai_tasks_admin_created": """
            SELECT id FROM ai_tasks
            WHERE created_at >= now() - interval '1 day'
            ORDER BY created_at DESC, id DESC LIMIT 30
        """,
        "ix_ai_tasks_admin_owner_created": """
            SELECT id FROM ai_tasks
            WHERE user_id = :user_id AND workspace_id = :workspace_id
            ORDER BY created_at DESC, id DESC LIMIT 30
        """,
        "ix_usage_events_admin_created": """
            SELECT id FROM usage_events
            WHERE created_at >= now() - interval '1 day'
            ORDER BY created_at DESC, id DESC LIMIT 30
        """,
        "ix_ticket_ledger_user_created": """
            SELECT id FROM ticket_ledger
            WHERE user_id = :user_id
            ORDER BY created_at DESC, id DESC LIMIT 30
        """,
        "ix_assets_admin_owner_updated": """
            SELECT id FROM assets
            WHERE user_id = :user_id AND workspace_id = :workspace_id
              AND asset_type = 'scene'
            ORDER BY updated_at DESC, id DESC LIMIT 30
        """,
        "ix_audit_events_target_action_created": """
            SELECT id FROM audit_events
            WHERE target_user_id = :user_id AND workspace_id = :workspace_id
              AND action = 'admin.rls.verification'
            ORDER BY created_at DESC, id DESC LIMIT 30
        """,
    }
    parameters = {
        "user_id": first_user_id,
        "workspace_id": first_workspace_id,
    }
    for expected_index, query in query_plans.items():
        plan = "\n".join(
            connection.scalars(text(f"EXPLAIN (COSTS OFF) {query}"), parameters)
        )
        assert expected_index in plan, (
            f"管理员查询未使用预期索引 {expected_index}:\n{plan}"
        )


def _seed_rls_fixtures(
    connection,
    *,
    admin_id: int,
) -> dict[str, tuple[int, int]]:
    fixture_ids: dict[str, list[int]] = {table: [] for table in RLS_TABLES}
    user_ids: list[int] = []
    workspace_ids: list[int] = []
    for sequence in (1, 2):
        user_id = connection.scalar(
            text(
                "INSERT INTO users "
                "(username, password_hash, status) "
                "VALUES (:username, 'rls-fixture', 'active') RETURNING id"
            ),
            {"username": f"rls_user_{sequence}"},
        )
        workspace_id = connection.scalar(
            text(
                "INSERT INTO workspaces (user_id, name, version) "
                "VALUES (:user_id, :name, 1) RETURNING id"
            ),
            {"user_id": user_id, "name": f"RLS 工作区 {sequence}"},
        )
        asset_id = connection.scalar(
            text(
                "INSERT INTO assets "
                "(user_id, workspace_id, scope, asset_type, name, payload, provenance, "
                "schema_version, version) VALUES "
                "(:user_id, :workspace_id, 'workspace', 'scene', :name, "
                "'{}'::jsonb, '{}'::jsonb, 1, 1) RETURNING id"
            ),
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "name": f"RLS 场景 {sequence}",
            },
        )
        media_id = connection.scalar(
            text(
                "INSERT INTO media_objects "
                "(user_id, workspace_id, scope, object_key, mime_type, size_bytes, "
                "checksum_sha256, lifecycle_state, provenance) VALUES "
                "(:user_id, :workspace_id, 'user', :object_key, 'image/png', 1, "
                ":checksum, 'active', '{}'::jsonb) RETURNING id"
            ),
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "object_key": f"rls/{sequence}/image.png",
                "checksum": str(sequence) * 64,
            },
        )
        task_id = connection.scalar(
            text(
                "INSERT INTO ai_tasks "
                "(user_id, workspace_id, capability, status, idempotency_key, "
                "request_fingerprint, request_payload, config_snapshot, "
                "tokens_per_ticket, quoted_microtickets, provider_billable) VALUES "
                "(:user_id, :workspace_id, 'image.t2i', 'queued', :idempotency_key, "
                ":fingerprint, '{}'::jsonb, '{}'::jsonb, 1000, 1000, false) "
                "RETURNING id"
            ),
            {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "idempotency_key": f"rls-task-{sequence}",
                "fingerprint": str(sequence) * 64,
            },
        )
        wallet_id = connection.scalar(
            text(
                "INSERT INTO ticket_wallets "
                "(user_id, available_microtickets, held_microtickets, "
                "lifetime_granted_microtickets, lifetime_spent_microtickets, "
                "lifetime_recharged_microtickets, lifetime_refunded_microtickets, version) "
                "VALUES (:user_id, 1000, 0, 1000, 0, 0, 0, 1) RETURNING id"
            ),
            {"user_id": user_id},
        )
        ledger_id = connection.scalar(
            text(
                "INSERT INTO ticket_ledger "
                "(user_id, workspace_id, entry_type, amount_microtickets, "
                "available_delta, held_delta, available_after, held_after, reason, "
                "correlation) VALUES "
                "(:user_id, :workspace_id, 'grant', 1000, 1000, 0, 1000, 0, "
                "'RLS 验证', '{}'::jsonb) RETURNING id"
            ),
            {"user_id": user_id, "workspace_id": workspace_id},
        )
        order_id = connection.scalar(
            text(
                "INSERT INTO manual_recharge_orders "
                "(order_number, user_id, cash_amount_fen, ticket_amount_microtickets, "
                "currency, status, exchange_snapshot, create_reason, "
                "refunded_cash_fen, refunded_microtickets, create_idempotency_key, "
                "create_request_fingerprint, version, created_by_admin_id) VALUES "
                "(:order_number, :user_id, 100, 1000, 'CNY', 'pending', "
                "'{}'::jsonb, 'RLS 验证', 0, 0, :idempotency_key, :fingerprint, 1, "
                ":admin_id) RETURNING id"
            ),
            {
                "admin_id": admin_id,
                "order_number": f"RLS-ORDER-{sequence}",
                "user_id": user_id,
                "idempotency_key": f"rls-order-{sequence}",
                "fingerprint": str(sequence) * 64,
            },
        )
        user_ids.append(user_id)
        workspace_ids.append(workspace_id)
        for table, record_id in (
            ("assets", asset_id),
            ("media_objects", media_id),
            ("ai_tasks", task_id),
            ("ticket_wallets", wallet_id),
            ("ticket_ledger", ledger_id),
            ("manual_recharge_orders", order_id),
        ):
            fixture_ids[table].append(record_id)
    fixture_ids["users"] = user_ids
    fixture_ids["workspaces"] = workspace_ids
    return {table: (ids[0], ids[1]) for table, ids in fixture_ids.items()}


def _assert_application_role(connection, application_role: str) -> None:
    role = connection.execute(
        text(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = :role"
        ),
        {"role": application_role},
    ).one_or_none()
    assert role is not None, f"缺少数据库应用角色：{application_role}"
    assert not any(role), f"数据库应用角色权限过高：{application_role}"


def _grant_rls_verification_access(connection, application_role: str) -> None:
    quoted_role = connection.dialect.identifier_preparer.quote(application_role)
    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
    connection.exec_driver_sql(
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {quoted_role}"
    )


def _set_rls_user_context(
    connection,
    *,
    application_role: str,
    user_id: int,
) -> None:
    quoted_role = connection.dialect.identifier_preparer.quote(application_role)
    connection.exec_driver_sql(f"SET LOCAL ROLE {quoted_role}")
    connection.execute(
        text(
            "SELECT set_config('app.current_user_id', :user_id, true), "
            "set_config('app.current_session_id', :session_id, true), "
            "set_config('app.current_admin_id', '', true), "
            "set_config('app.current_admin_session_id', '', true)"
        ),
        {
            "user_id": str(user_id),
            "session_id": f"rls-verification-{user_id}",
        },
    )


def _set_rls_admin_context(
    connection,
    *,
    application_role: str,
    admin_id: int,
) -> None:
    quoted_role = connection.dialect.identifier_preparer.quote(application_role)
    connection.exec_driver_sql(f"SET LOCAL ROLE {quoted_role}")
    connection.execute(
        text(
            "SELECT set_config('app.current_user_id', '', true), "
            "set_config('app.current_session_id', '', true), "
            "set_config('app.current_admin_id', :admin_id, true), "
            "set_config('app.current_admin_session_id', :session_id, true)"
        ),
        {
            "admin_id": str(admin_id),
            "session_id": f"admin-rls-verification-{admin_id}",
        },
    )


def _visible_fixture_ids(
    connection,
    table: str,
    fixture_ids: tuple[int, int],
) -> tuple[int, ...]:
    first_id, second_id = fixture_ids
    return tuple(
        connection.scalars(
            text(
                f'SELECT id FROM "{table}" '
                "WHERE id IN (:first_id, :second_id) ORDER BY id"
            ),
            {"first_id": first_id, "second_id": second_id},
        )
    )


def _assert_rls_behavior(
    database_url: str,
    application_role: str,
    admin_id: int,
    fixtures: dict[str, tuple[int, int]],
) -> None:
    verification_engine = create_engine(database_url, pool_size=1, max_overflow=0)
    audit_correlation_id = f"rls-user-audit-{fixtures['users'][0]}"
    try:
        with verification_engine.begin() as connection:
            _set_rls_user_context(
                connection,
                application_role=application_role,
                user_id=fixtures["users"][0],
            )
            for table, fixture_ids in fixtures.items():
                assert _visible_fixture_ids(connection, table, fixture_ids) == (
                    fixture_ids[0],
                ), f"普通用户 RLS 越界：{table}"
            connection.execute(
                text(
                    "INSERT INTO audit_events "
                    "(actor_user_id, target_user_id, action, target_type, target_id, "
                    "correlation_id) VALUES "
                    "(:user_id, :user_id, 'auth.login', 'session', 'rls-session', "
                    ":correlation_id)"
                ),
                {
                    "user_id": fixtures["users"][0],
                    "correlation_id": audit_correlation_id,
                },
            )
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE correlation_id = :correlation_id"
                ),
                {"correlation_id": audit_correlation_id},
            ) == 0, "普通用户不应读取审计事件"

        with verification_engine.begin() as connection:
            _set_rls_admin_context(
                connection,
                application_role=application_role,
                admin_id=admin_id,
            )
            for table, fixture_ids in fixtures.items():
                assert _visible_fixture_ids(connection, table, fixture_ids) == tuple(
                    sorted(fixture_ids)
                ), f"管理员 RLS 可见性缺失：{table}"
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE correlation_id = :correlation_id"
                ),
                {"correlation_id": audit_correlation_id},
            ) == 1, "管理员应能读取用户写入的审计事件"

        with verification_engine.begin() as connection:
            quoted_role = connection.dialect.identifier_preparer.quote(application_role)
            connection.exec_driver_sql(f"SET LOCAL ROLE {quoted_role}")
            settings = connection.execute(
                text(
                    "SELECT current_setting('app.current_user_id', true), "
                    "current_setting('app.current_session_id', true), "
                    "current_setting('app.current_admin_id', true), "
                    "current_setting('app.current_admin_session_id', true), "
                    "current_setting('app.admin_login_username', true), "
                    "current_setting('app.admin_session_token_hash', true)"
                )
            ).one()
            assert all(value in (None, "") for value in settings), (
                "复用数据库连接残留了管理员事务上下文"
            )
            assert connection.scalar(text("SELECT count(*) FROM users")) == 0
    finally:
        verification_engine.dispose()


def _assert_manual_recharge_concurrency(
    database_url: str,
    admin_id: int,
    user_id: int,
) -> None:
    database = Database(database_url)
    service = ManualRechargeOrderService(database)
    admin = AdminContext(
        admin_id=str(admin_id),
        session_id="rls-concurrency-admin",
        username="rls_admin",
    )
    try:
        order = service.create_pending(
            admin,
            user_id=user_id,
            cash_amount_fen=100,
            ticket_amount_microtickets=1000,
            offline_reference="RLS-CONCURRENCY-ORDER",
            reason="财务登记并发验证订单",
            idempotency_key="rls-concurrency-create",
            exchange_snapshot={"tokens_per_ticket": 1000},
        )

        def complete_order():
            return service.complete(
                admin,
                order.id,
                expected_version=1,
                reason="财务并发确认线下到账",
                idempotency_key="rls-concurrency-complete",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            completed = list(executor.map(lambda _index: complete_order(), range(2)))
        assert {result.status for result in completed} == {"completed"}

        def refund_order():
            return service.refund(
                admin,
                order.id,
                cash_amount_fen=100,
                ticket_amount_microtickets=1000,
                expected_version=2,
                reason="财务并发登记全额退款",
                idempotency_key="rls-concurrency-refund",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            refunded = list(executor.map(lambda _index: refund_order(), range(2)))
        assert {result.status for result in refunded} == {"refunded"}

        with database.transaction(admin) as session:
            wallet = session.scalar(
                select(TicketWalletRecord).where(TicketWalletRecord.user_id == user_id)
            )
            assert wallet is not None
            assert wallet.available_microtickets == 1000
            assert wallet.held_microtickets == 0
            assert session.scalar(
                select(func.count(TicketLedgerRecord.id)).where(
                    TicketLedgerRecord.manual_recharge_order_id == order.id,
                    TicketLedgerRecord.entry_type == "manual_recharge",
                )
            ) == 1
            assert session.scalar(
                select(func.count(TicketLedgerRecord.id)).where(
                    TicketLedgerRecord.manual_recharge_order_id == order.id,
                    TicketLedgerRecord.entry_type == "manual_recharge_refund",
                )
            ) == 1
            assert session.scalar(
                select(func.count(ManualRechargeOrderEventRecord.id)).where(
                    ManualRechargeOrderEventRecord.order_id == order.id,
                    ManualRechargeOrderEventRecord.event_type == "completed",
                )
            ) == 1
            assert session.scalar(
                select(func.count(ManualRechargeOrderEventRecord.id)).where(
                    ManualRechargeOrderEventRecord.order_id == order.id,
                    ManualRechargeOrderEventRecord.event_type == "refunded",
                )
            ) == 1
    finally:
        database.dispose()


def main() -> int:
    raw_url = os.getenv("LUMENX_DATABASE_URL")
    if not raw_url:
        raise RuntimeError("迁移验证缺少 PostgreSQL 管理连接")
    base_url = make_url(raw_url)
    if base_url.get_backend_name() != "postgresql":
        raise RuntimeError("迁移验证只能使用 PostgreSQL")
    if base_url.database in DATABASE_NAMES:
        raise RuntimeError("管理连接不能指向待重建的迁移验证数据库")
    application_role = os.getenv("LUMENX_DATABASE_USER", "lumenx_app")

    admin_url = base_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    created_databases: list[str] = []
    try:
        with admin_engine.connect() as connection:
            for database_name in DATABASE_NAMES:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
                connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
                created_databases.append(database_name)

        fresh_url = _database_url(base_url, DATABASE_NAMES[0])
        legacy_url = _database_url(base_url, DATABASE_NAMES[1])
        _run_alembic(fresh_url, "head")
        fresh_engine = create_engine(fresh_url)
        try:
            with fresh_engine.connect() as connection:
                _assert_database_identifier_policy(connection)
                _assert_auth_rls_contract(connection)
                _assert_admin_console_schema(connection)
                _assert_application_role(connection, application_role)
                fresh_active = connection.execute(
                    text(
                        "SELECT cv.created_by_user_id, cv.created_by_admin_id, pc.settings "
                        "FROM config_versions AS cv "
                        "JOIN platform_configs AS pc ON pc.config_version_id = cv.id "
                        "WHERE cv.status = 'active'"
                    )
                ).one()
                assert fresh_active.created_by_user_id == 0
                assert fresh_active.created_by_admin_id is None
                assert fresh_active.settings["exposed_capabilities"] == []
                assert fresh_active.settings["feature_flags"] == {
                    "registration_mode": "disabled",
                    "new_ai_tasks_enabled": False,
                }
            with fresh_engine.begin() as connection:
                _grant_rls_verification_access(connection, application_role)
                admin_id = connection.scalar(
                    text(
                        "INSERT INTO admin_users "
                        "(username, password_hash, status, must_change_password) "
                        "VALUES ('rls_admin', 'rls-fixture', 'active', true) RETURNING id"
                    )
                )
                fixtures = _seed_rls_fixtures(connection, admin_id=admin_id)
                _assert_admin_query_plans(connection, fixtures)
            _assert_rls_behavior(
                fresh_url,
                application_role,
                admin_id,
                fixtures,
            )
            _assert_manual_recharge_concurrency(
                fresh_url,
                admin_id,
                fixtures["users"][0],
            )
        finally:
            fresh_engine.dispose()

        _run_alembic(legacy_url, "0008_audit_event_insert_policy")
        legacy_engine = create_engine(legacy_url)
        try:
            with legacy_engine.begin() as connection:
                admin_id = connection.scalar(
                    text(
                        "INSERT INTO users "
                        "(phone_canonical, password_hash, status, is_platform_admin) "
                        "VALUES ('13800138999', 'legacy-fixture', 'active', true) "
                        "RETURNING id"
                    )
                )
                workspace_id = connection.scalar(
                    text(
                        "INSERT INTO workspaces (user_id, name, version) "
                        "VALUES (:admin_id, 'legacy workspace', 1) RETURNING id"
                    ),
                    {"admin_id": admin_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO auth_sessions "
                        "(user_id, token_hash, csrf_token_hash, idle_expires_at, "
                        "absolute_expires_at) VALUES "
                        "(:admin_id, :token_hash, :csrf_token_hash, "
                        "now() + interval '1 day', now() + interval '2 days')"
                    ),
                    {
                        "admin_id": admin_id,
                        "token_hash": "b" * 64,
                        "csrf_token_hash": "c" * 64,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO media_objects "
                        "(user_id, workspace_id, object_key, mime_type, size_bytes, "
                        "checksum_sha256, lifecycle_state, provenance) "
                        "VALUES (:admin_id, :workspace_id, 'legacy/object.png', 'image/png', "
                        "1, :checksum, 'active', '{}'::jsonb)"
                    ),
                    {
                        "admin_id": admin_id,
                        "workspace_id": workspace_id,
                        "checksum": "a" * 64,
                    },
                )
                config_id = connection.scalar(
                    text(
                        "INSERT INTO config_versions "
                        "(version_number, status, schema_version, created_by_user_id, "
                        "reason, activated_at) "
                        "VALUES (1, 'active', 1, :admin_id, "
                        "'legacy registration fixture', now()) RETURNING id"
                    ),
                    {"admin_id": admin_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO platform_configs "
                        "(config_version_id, tokens_per_ticket, "
                        "registration_initial_grant_microtickets, session_idle_seconds, "
                        "session_absolute_seconds, max_sessions_per_user, "
                        "max_ai_concurrency_per_user, settings) "
                        "VALUES (:config_id, 1000, 0, 3600, 86400, 5, 2, "
                        "CAST(:settings AS jsonb))"
                    ),
                    {
                        "config_id": config_id,
                        "settings": json.dumps(
                            {
                                "feature_flags": {
                                    "registration_enabled": True,
                                    "new_ai_tasks_enabled": False,
                                },
                                "operational": {
                                    "signed_media_url_seconds": 300,
                                    "soft_delete_retention_days": 30,
                                    "stale_hold_minutes": 30,
                                    "global_worker_concurrency": 8,
                                },
                                "exposed_capabilities": ["image.t2i"],
                            }
                        ),
                    },
                )
        finally:
            legacy_engine.dispose()

        _run_alembic(legacy_url, "head")
        verification_engine = create_engine(legacy_url)
        try:
            with verification_engine.connect() as connection:
                _assert_database_identifier_policy(connection)
                _assert_auth_rls_contract(connection)
                _assert_admin_console_schema(connection)
                settings = connection.scalar(
                    text(
                        "SELECT settings FROM platform_configs "
                        "WHERE config_version_id = :config_id"
                    ),
                    {"config_id": config_id},
                )
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
                active_versions = connection.scalar(
                    text("SELECT count(*) FROM config_versions WHERE status = 'active'")
                )
                invitation_table = connection.scalar(
                    text("SELECT to_regclass('public.registration_invitations')")
                )
                session_columns = set(
                    connection.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'auth_sessions'"
                        )
                    )
                )
                legacy_media_scope = connection.scalar(
                    text(
                        "SELECT scope FROM media_objects "
                        "WHERE object_key = 'legacy/object.png'"
                    )
                )
                former_admin = connection.execute(
                    text(
                        "SELECT phone_canonical, status FROM users WHERE id = :user_id"
                    ),
                    {"user_id": admin_id},
                ).one()
                preserved_workspace_count = connection.scalar(
                    text(
                        "SELECT count(*) FROM workspaces WHERE id = :workspace_id "
                        "AND user_id = :user_id"
                    ),
                    {"workspace_id": workspace_id, "user_id": admin_id},
                )
                independent_admin_count = connection.scalar(
                    text("SELECT count(*) FROM admin_users")
                )
                legacy_session_revoked_at = connection.scalar(
                    text(
                        "SELECT revoked_at FROM auth_sessions WHERE token_hash = :token_hash"
                    ),
                    {"token_hash": "b" * 64},
                )
            feature_flags = settings["feature_flags"]
            operational = settings["operational"]
            assert feature_flags["registration_mode"] == "invite_only"
            assert active_versions == 1
            assert "registration_enabled" not in feature_flags
            assert "global_worker_concurrency" not in operational
            assert invitation_table == "registration_invitations"
            assert {"idle_timeout_seconds", "configuration_version_id"} <= session_columns
            assert legacy_media_scope == "user"
            assert former_admin.phone_canonical == "13800138999"
            assert former_admin.status == "active"
            assert preserved_workspace_count == 1
            assert independent_admin_count == 0
            assert legacy_session_revoked_at is not None
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "fresh_database": DATABASE_NAMES[0],
                        "legacy_database": DATABASE_NAMES[1],
                        "migration_head": revision,
                        "legacy_registration_mode": feature_flags["registration_mode"],
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            verification_engine.dispose()
        return 0
    finally:
        try:
            if created_databases:
                with admin_engine.connect() as connection:
                    for database_name in created_databases:
                        connection.execute(
                            text(
                                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                                "WHERE datname = :database_name "
                                "AND pid <> pg_backend_pid()"
                            ),
                            {"database_name": database_name},
                        )
                        connection.exec_driver_sql(
                            f'DROP DATABASE IF EXISTS "{database_name}"'
                        )
        finally:
            admin_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
