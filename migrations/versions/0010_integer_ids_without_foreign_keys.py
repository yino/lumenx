"""Convert legacy UUID identifiers to bigint identities and remove foreign keys."""

from __future__ import annotations

from collections.abc import Sequence
import re

from alembic import op
import sqlalchemy as sa


revision: str = "0010_integer_ids_no_fks"
down_revision: str | None = "0009_controlled_registration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ID_TABLES = (
    "users",
    "workspaces",
    "auth_sessions",
    "password_reset_credentials",
    "series",
    "projects",
    "media_objects",
    "assets",
    "ai_tasks",
    "ai_task_attempts",
    "ticket_holds",
    "ticket_ledger",
    "usage_events",
    "config_versions",
    "model_configs",
    "platform_configs",
    "audit_events",
    "import_batches",
    "import_batch_items",
    "imported_playground_history",
    "registration_invitations",
)

UUID_COLUMN_TARGETS = {
    "users": {"id": "users"},
    "workspaces": {"id": "workspaces", "user_id": "users"},
    "auth_sessions": {
        "id": "auth_sessions",
        "user_id": "users",
        "configuration_version_id": "config_versions",
    },
    "password_reset_credentials": {
        "id": "password_reset_credentials",
        "user_id": "users",
        "created_by_admin_id": "users",
    },
    "series": {
        "id": "series",
        "user_id": "users",
        "workspace_id": "workspaces",
    },
    "projects": {
        "id": "projects",
        "user_id": "users",
        "workspace_id": "workspaces",
        "series_id": "series",
    },
    "media_objects": {
        "id": "media_objects",
        "user_id": "users",
        "workspace_id": "workspaces",
        "project_id": "projects",
    },
    "assets": {
        "id": "assets",
        "user_id": "users",
        "workspace_id": "workspaces",
        "project_id": "projects",
        "series_id": "series",
        "media_object_id": "media_objects",
    },
    "ticket_wallets": {"user_id": "users"},
    "ai_tasks": {
        "id": "ai_tasks",
        "user_id": "users",
        "workspace_id": "workspaces",
        "project_id": "projects",
    },
    "ai_task_attempts": {
        "id": "ai_task_attempts",
        "user_id": "users",
        "workspace_id": "workspaces",
        "task_id": "ai_tasks",
        "retry_of_attempt_id": "ai_task_attempts",
    },
    "ticket_holds": {
        "id": "ticket_holds",
        "user_id": "users",
        "workspace_id": "workspaces",
        "task_id": "ai_tasks",
        "attempt_id": "ai_task_attempts",
    },
    "ticket_ledger": {
        "id": "ticket_ledger",
        "user_id": "users",
        "workspace_id": "workspaces",
        "project_id": "projects",
        "task_id": "ai_tasks",
        "hold_id": "ticket_holds",
        "actor_user_id": "users",
    },
    "usage_events": {
        "id": "usage_events",
        "user_id": "users",
        "workspace_id": "workspaces",
        "project_id": "projects",
        "task_id": "ai_tasks",
        "attempt_id": "ai_task_attempts",
    },
    "config_versions": {"id": "config_versions", "created_by_user_id": "users"},
    "model_configs": {"id": "model_configs", "config_version_id": "config_versions"},
    "platform_configs": {
        "id": "platform_configs",
        "config_version_id": "config_versions",
    },
    "audit_events": {
        "id": "audit_events",
        "actor_user_id": "users",
        "target_user_id": "users",
        "workspace_id": "workspaces",
    },
    "import_batches": {
        "id": "import_batches",
        "actor_admin_user_id": "users",
        "target_user_id": "users",
        "target_workspace_id": "workspaces",
    },
    "import_batch_items": {
        "id": "import_batch_items",
        "batch_id": "import_batches",
        "user_id": "users",
        "workspace_id": "workspaces",
    },
    "imported_playground_history": {
        "id": "imported_playground_history",
        "user_id": "users",
        "workspace_id": "workspaces",
        "import_batch_id": "import_batches",
    },
    "registration_invitations": {
        "id": "registration_invitations",
        "created_by_admin_id": "users",
        "consumed_by_user_id": "users",
    },
}

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_POLICY_COMMANDS = {"ALL", "SELECT", "INSERT", "UPDATE", "DELETE"}


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise RuntimeError(f"Unsafe database identifier: {value!r}")
    return f'"{value}"'


def _drop_foreign_keys(connection) -> None:
    constraints = connection.execute(
        sa.text(
            """
            SELECT relation.relname AS table_name, constraint_record.conname AS constraint_name
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public' AND constraint_record.contype = 'f'
            ORDER BY relation.relname, constraint_record.conname
            """
        )
    ).mappings()
    for constraint in constraints:
        connection.exec_driver_sql(
            f"ALTER TABLE {_quote_identifier(constraint['table_name'])} "
            f"DROP CONSTRAINT {_quote_identifier(constraint['constraint_name'])}"
        )


def _load_and_drop_policies(connection) -> list[dict[str, object]]:
    policies = [
        dict(row)
        for row in connection.execute(
            sa.text(
                """
                SELECT tablename, policyname, permissive, roles, cmd, qual, with_check
                FROM pg_policies
                WHERE schemaname = 'public'
                ORDER BY tablename, policyname
                """
            )
        ).mappings()
    ]
    for policy in policies:
        connection.exec_driver_sql(
            f"DROP POLICY {_quote_identifier(str(policy['policyname']))} "
            f"ON {_quote_identifier(str(policy['tablename']))}"
        )
    return policies


def _restore_policies(connection, policies: list[dict[str, object]]) -> None:
    for policy in policies:
        command = str(policy["cmd"]).upper()
        if command not in _POLICY_COMMANDS:
            raise RuntimeError(f"Unsupported policy command: {command}")
        roles = policy.get("roles") or ["public"]
        rendered_roles = ", ".join(
            "PUBLIC" if str(role).lower() == "public" else _quote_identifier(str(role))
            for role in roles
        )
        statement = (
            f"CREATE POLICY {_quote_identifier(str(policy['policyname']))} "
            f"ON {_quote_identifier(str(policy['tablename']))} "
            f"AS {str(policy['permissive']).upper()} FOR {command} TO {rendered_roles}"
        )
        if policy.get("qual"):
            expression = str(policy["qual"]).replace("::uuid", "::bigint")
            statement += f" USING ({expression})"
        if policy.get("with_check"):
            expression = str(policy["with_check"]).replace("::uuid", "::bigint")
            statement += f" WITH CHECK ({expression})"
        connection.exec_driver_sql(statement)


def _application_tables(connection) -> list[str]:
    return list(
        connection.scalars(
            sa.text(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name <> 'alembic_version'
                ORDER BY table_name
                """
            )
        )
    )


def _uuid_columns(connection) -> set[tuple[str, str]]:
    return {
        (str(row.table_name), str(row.column_name))
        for row in connection.execute(
            sa.text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND data_type = 'uuid'
                """
            )
        )
    }


def _reset_identity_sequence(connection, table_name: str) -> None:
    sequence_name = connection.scalar(
        sa.text("SELECT pg_get_serial_sequence(:table_name, 'id')"),
        {"table_name": f"public.{table_name}"},
    )
    next_id = connection.scalar(
        sa.text(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {_quote_identifier(table_name)}")
    )
    connection.execute(
        sa.text("SELECT setval(CAST(:sequence_name AS regclass), :next_id, false)"),
        {"sequence_name": sequence_name, "next_id": next_id},
    )


def _assert_final_schema(connection) -> None:
    remaining_uuid_columns = _uuid_columns(connection)
    remaining_foreign_keys = connection.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public' AND constraint_record.contype = 'f'
            """
        )
    )
    invalid_identifiers = connection.execute(
        sa.text(
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
            SELECT app_tables.table_name
            FROM app_tables
            LEFT JOIN primary_keys USING (table_name)
            LEFT JOIN information_schema.columns AS identifier
              ON identifier.table_schema = 'public'
             AND identifier.table_name = app_tables.table_name
             AND identifier.column_name = 'id'
            WHERE primary_keys.columns IS DISTINCT FROM ARRAY['id']::text[]
               OR identifier.data_type <> 'bigint'
               OR identifier.is_identity <> 'YES'
            ORDER BY app_tables.table_name
            """
        )
    ).all()
    if remaining_uuid_columns or remaining_foreign_keys or invalid_identifiers:
        raise RuntimeError(
            "Identifier migration verification failed: "
            f"uuid_columns={sorted(remaining_uuid_columns)}, "
            f"foreign_keys={remaining_foreign_keys}, "
            f"invalid_identifiers={invalid_identifiers}"
        )


def upgrade() -> None:
    connection = op.get_bind()
    _drop_foreign_keys(connection)
    legacy_uuid_columns = _uuid_columns(connection)
    if not legacy_uuid_columns:
        _assert_final_schema(connection)
        return

    expected_uuid_columns = {
        (table_name, column_name)
        for table_name, columns in UUID_COLUMN_TARGETS.items()
        for column_name in columns
    }
    unknown_uuid_columns = legacy_uuid_columns - expected_uuid_columns
    if unknown_uuid_columns:
        raise RuntimeError(
            f"Legacy schema contains unmapped UUID columns: {sorted(unknown_uuid_columns)}"
        )

    policies = _load_and_drop_policies(connection)
    tables = _application_tables(connection)
    for table_name in tables:
        connection.exec_driver_sql(
            f"ALTER TABLE {_quote_identifier(table_name)} DISABLE TRIGGER USER"
        )

    connection.exec_driver_sql("DROP FUNCTION IF EXISTS lumenx_current_user_id()")
    connection.exec_driver_sql(
        """
        CREATE TEMP TABLE lumenx_uuid_id_map (
            table_name text NOT NULL,
            old_id uuid NOT NULL,
            new_id bigint NOT NULL,
            PRIMARY KEY (table_name, old_id),
            UNIQUE (table_name, new_id)
        ) ON COMMIT DROP
        """
    )
    for table_name in ID_TABLES:
        connection.execute(
            sa.text(
                f"INSERT INTO lumenx_uuid_id_map (table_name, old_id, new_id) "
                f"SELECT :table_name, id, row_number() OVER (ORDER BY id) "
                f"FROM {_quote_identifier(table_name)}"
            ),
            {"table_name": table_name},
        )
    connection.exec_driver_sql(
        """
        CREATE FUNCTION pg_temp.lumenx_uuid_to_bigint(target_table text, source_id uuid)
        RETURNS bigint
        LANGUAGE sql STABLE PARALLEL RESTRICTED
        AS $$
            SELECT new_id
            FROM pg_temp.lumenx_uuid_id_map
            WHERE table_name = target_table AND old_id = source_id
        $$
        """
    )

    for table_name, columns in UUID_COLUMN_TARGETS.items():
        for column_name, target_table in columns.items():
            if (table_name, column_name) not in legacy_uuid_columns:
                continue
            connection.exec_driver_sql(
                f"ALTER TABLE {_quote_identifier(table_name)} "
                f"ALTER COLUMN {_quote_identifier(column_name)} DROP DEFAULT, "
                f"ALTER COLUMN {_quote_identifier(column_name)} TYPE bigint "
                f"USING pg_temp.lumenx_uuid_to_bigint('{target_table}', "
                f"{_quote_identifier(column_name)})"
            )

    for table_name in ID_TABLES:
        connection.exec_driver_sql(
            f"ALTER TABLE {_quote_identifier(table_name)} "
            "ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY"
        )

    wallet_primary_key = connection.scalar(
        sa.text(
            """
            SELECT constraint_record.conname
            FROM pg_constraint AS constraint_record
            JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relname = 'ticket_wallets'
              AND constraint_record.contype = 'p'
            """
        )
    )
    connection.exec_driver_sql(
        "ALTER TABLE ticket_wallets ADD COLUMN id bigint "
        "GENERATED BY DEFAULT AS IDENTITY"
    )
    connection.exec_driver_sql(
        f"ALTER TABLE ticket_wallets DROP CONSTRAINT "
        f"{_quote_identifier(str(wallet_primary_key))}"
    )
    connection.exec_driver_sql(
        "ALTER TABLE ticket_wallets ADD CONSTRAINT pk_ticket_wallets PRIMARY KEY (id)"
    )
    connection.exec_driver_sql(
        "ALTER TABLE ticket_wallets ADD CONSTRAINT uq_ticket_wallets_user_id UNIQUE (user_id)"
    )

    for table_name in (*ID_TABLES, "ticket_wallets"):
        _reset_identity_sequence(connection, table_name)

    connection.exec_driver_sql(
        """
        CREATE FUNCTION pg_temp.lumenx_rewrite_json_ids(source jsonb)
        RETURNS jsonb
        LANGUAGE plpgsql STABLE PARALLEL RESTRICTED
        AS $$
        DECLARE
            replacement bigint;
            rewritten jsonb;
        BEGIN
            IF source IS NULL THEN
                RETURN NULL;
            END IF;
            CASE jsonb_typeof(source)
                WHEN 'string' THEN
                    SELECT new_id INTO replacement
                    FROM pg_temp.lumenx_uuid_id_map
                    WHERE old_id::text = source #>> '{}'
                    ORDER BY table_name
                    LIMIT 1;
                    IF replacement IS NOT NULL THEN
                        RETURN to_jsonb(replacement::text);
                    END IF;
                WHEN 'array' THEN
                    SELECT COALESCE(
                        jsonb_agg(pg_temp.lumenx_rewrite_json_ids(item) ORDER BY ordinality),
                        '[]'::jsonb
                    ) INTO rewritten
                    FROM jsonb_array_elements(source) WITH ORDINALITY AS elements(item, ordinality);
                    RETURN rewritten;
                WHEN 'object' THEN
                    SELECT COALESCE(
                        jsonb_object_agg(key, pg_temp.lumenx_rewrite_json_ids(value)),
                        '{}'::jsonb
                    ) INTO rewritten
                    FROM jsonb_each(source);
                    RETURN rewritten;
                ELSE
                    RETURN source;
            END CASE;
            RETURN source;
        END;
        $$
        """
    )
    json_columns = connection.execute(
        sa.text(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND data_type = 'jsonb'
            ORDER BY table_name, ordinal_position
            """
        )
    ).mappings()
    for column in json_columns:
        connection.exec_driver_sql(
            f"UPDATE {_quote_identifier(str(column['table_name']))} "
            f"SET {_quote_identifier(str(column['column_name']))} = "
            f"pg_temp.lumenx_rewrite_json_ids({_quote_identifier(str(column['column_name']))}) "
            f"WHERE {_quote_identifier(str(column['column_name']))} IS NOT NULL"
        )
    connection.exec_driver_sql(
        "UPDATE series SET payload = jsonb_set(payload, '{id}', to_jsonb(id::text), true)"
    )
    connection.exec_driver_sql(
        "UPDATE projects SET payload = "
        "jsonb_set(jsonb_set(payload, '{id}', to_jsonb(id::text), true), "
        "'{series_id}', COALESCE(to_jsonb(series_id::text), 'null'::jsonb), true)"
    )
    for table_name, column_name in (
        ("audit_events", "target_id"),
        ("import_batch_items", "target_id"),
    ):
        connection.exec_driver_sql(
            f"UPDATE {_quote_identifier(table_name)} AS target "
            f"SET {_quote_identifier(column_name)} = mapping.new_id::text "
            "FROM pg_temp.lumenx_uuid_id_map AS mapping "
            f"WHERE target.{_quote_identifier(column_name)} = mapping.old_id::text"
        )

    connection.exec_driver_sql(
        """
        CREATE FUNCTION lumenx_current_user_id() RETURNS bigint
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.current_user_id', true), '')::bigint
        $$
        """
    )
    _restore_policies(connection, policies)
    for table_name in tables:
        connection.exec_driver_sql(
            f"ALTER TABLE {_quote_identifier(table_name)} ENABLE TRIGGER USER"
        )
    _assert_final_schema(connection)


def downgrade() -> None:
    raise RuntimeError(
        "The UUID-to-bigint identifier migration is intentionally irreversible"
    )
