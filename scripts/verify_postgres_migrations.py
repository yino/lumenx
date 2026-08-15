#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url


DATABASE_NAMES = ("lumenx_release_fresh", "lumenx_release_legacy")


def _database_url(base_url: URL, database_name: str) -> str:
    return base_url.set(database=database_name).render_as_string(
        hide_password=False
    )


def _run_alembic(database_url: str, target: str) -> None:
    environment = os.environ.copy()
    environment["LUMENX_DATABASE_URL"] = database_url
    subprocess.run(
        ["alembic", "upgrade", target],
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
        "lumenx_is_platform_admin",
        "lumenx_login_phone_canonical",
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
    assert "lumenx_registration_phone_canonical" in select_check


def main() -> int:
    raw_url = os.getenv("LUMENX_DATABASE_URL")
    if not raw_url:
        raise RuntimeError("迁移验证缺少 PostgreSQL 管理连接")
    base_url = make_url(raw_url)
    if base_url.get_backend_name() != "postgresql":
        raise RuntimeError("迁移验证只能使用 PostgreSQL")
    if base_url.database in DATABASE_NAMES:
        raise RuntimeError("管理连接不能指向待重建的迁移验证数据库")

    admin_url = base_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
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

        fresh_url = _database_url(base_url, DATABASE_NAMES[0])
        legacy_url = _database_url(base_url, DATABASE_NAMES[1])
        _run_alembic(fresh_url, "head")
        fresh_engine = create_engine(fresh_url)
        try:
            with fresh_engine.connect() as connection:
                _assert_database_identifier_policy(connection)
                _assert_auth_rls_contract(connection)
                fresh_active = connection.execute(
                    text(
                        "SELECT cv.created_by_user_id, pc.settings "
                        "FROM config_versions AS cv "
                        "JOIN platform_configs AS pc ON pc.config_version_id = cv.id "
                        "WHERE cv.status = 'active'"
                    )
                ).one()
                assert fresh_active.created_by_user_id == 0
                assert fresh_active.settings["exposed_capabilities"] == []
                assert fresh_active.settings["feature_flags"] == {
                    "registration_mode": "disabled",
                    "new_ai_tasks_enabled": False,
                }
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
            feature_flags = settings["feature_flags"]
            operational = settings["operational"]
            assert feature_flags["registration_mode"] == "invite_only"
            assert active_versions == 1
            assert "registration_enabled" not in feature_flags
            assert "global_worker_concurrency" not in operational
            assert invitation_table == "registration_invitations"
            assert {"idle_timeout_seconds", "configuration_version_id"} <= session_columns
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
        admin_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
