from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from scripts.verify_postgres_migrations import _database_url


def test_migration_revision_ids_fit_alembic_version_table_and_form_one_chain() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    revisions = list(scripts.walk_revisions(base="base", head="heads"))
    revision_ids = [revision.revision for revision in revisions]

    assert revisions
    assert len(revision_ids) == len(set(revision_ids))
    assert all(len(revision_id) <= 32 for revision_id in revision_ids)
    assert len(scripts.get_heads()) == 1


def test_migration_verifier_preserves_password_when_switching_databases() -> None:
    base_url = make_url(
        "postgresql+psycopg://lumenx:p%40ss%2Fword%3A2026@postgres/lumenx"
    )

    rendered = _database_url(base_url, "lumenx_release_fresh")
    parsed = make_url(rendered)

    assert "***" not in rendered
    assert parsed.password == "p@ss/word:2026"
    assert parsed.database == "lumenx_release_fresh"


def test_controlled_registration_migration_restores_config_immutability() -> None:
    migration = Path(
        "migrations/versions/0009_controlled_registration.py"
    ).read_text(encoding="utf-8")
    disabled_at = migration.index(
        "ALTER TABLE platform_configs DISABLE TRIGGER platform_configs_immutable"
    )
    registration_update_at = migration.index("UPDATE platform_configs")
    enabled_at = migration.index(
        "ALTER TABLE platform_configs ENABLE TRIGGER platform_configs_immutable"
    )

    assert disabled_at < registration_update_at < enabled_at


def test_safe_runtime_policy_migration_is_fail_closed_and_non_overwriting() -> None:
    migration = Path(
        "migrations/versions/0011_bootstrap_safe_runtime_policy.py"
    ).read_text(encoding="utf-8")

    assert "WHERE status = 'active'" in migration
    assert '"exposed_capabilities": []' in migration
    assert '"registration_mode": "disabled"' in migration
    assert '"new_ai_tasks_enabled": False' in migration


def test_auth_rls_repair_migration_restores_public_registration_contract() -> None:
    migration = Path(
        "migrations/versions/0012_repair_auth_rls.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0011_safe_runtime_policy"' in migration
    assert '"lumenx_registration_phone_canonical"' in migration
    assert "app.registration_phone_canonical" in migration
    assert '"users_insert_scope"' in migration
    assert "phone_canonical = lumenx_registration_phone_canonical()" in migration
    assert '"lumenx_login_phone_canonical"' in migration
    assert '"lumenx_session_token_hash"' in migration


def test_registration_returning_policy_exposes_only_the_pending_phone() -> None:
    migration = Path(
        "migrations/versions/0013_registration_returning_rls.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0012_repair_auth_rls"' in migration
    assert "CREATE POLICY users_select_scope ON users FOR SELECT" in migration
    assert "phone_canonical = lumenx_login_phone_canonical()" in migration
    assert "phone_canonical = lumenx_registration_phone_canonical()" in migration
    assert "OR true" not in migration.lower()


def test_admin_console_migration_adds_username_integer_financial_ids_and_no_foreign_keys() -> None:
    migration = Path(
        "migrations/versions/0014_expand_admin_console.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0013_registration_returning_rls"' in migration
    assert "lumenx_login_username" in migration
    assert "sa.Identity()" in migration
    assert "manual_recharge_orders" in migration
    assert "manual_recharge_order_events" in migration
    assert "financial history is immutable" in migration
    assert "ForeignKey" not in migration


def test_system_media_copy_access_migration_preserves_user_snapshot_media() -> None:
    migration = Path(
        "migrations/versions/0015_system_media_copy_access.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0014_expand_admin_console"' in migration
    assert "assets.user_id = lumenx_current_user_id()" in migration
    assert "assets.provenance->>'origin' = 'system_scene_copy'" in migration
    assert "lumenx_is_platform_admin()" in migration
    assert "ForeignKey" not in migration


def test_admin_query_index_migration_supports_bounded_console_queries() -> None:
    migration = Path(
        "migrations/versions/0016_admin_query_indexes.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0015_system_media_copy_access"' in migration
    for index_name in (
        "ix_users_admin_created",
        "ix_ai_tasks_admin_created",
        "ix_ai_tasks_admin_owner_created",
        "ix_usage_events_admin_created",
        "ix_assets_admin_owner_updated",
        "ix_media_objects_admin_owner_created",
        "ix_manual_recharge_orders_status_created",
    ):
        assert index_name in migration
    assert "ForeignKey" not in migration


def test_physical_admin_identity_migration_separates_users_and_privileged_actors() -> None:
    migration = Path(
        "migrations/versions/0017_physical_admin_identity.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0016_admin_query_indexes"' in migration
    assert '"admin_users"' in migration
    assert '"admin_sessions"' in migration
    assert "app.current_admin_id" in migration
    assert "AS $$ SELECT lumenx_has_admin_context() $$" in migration
    assert 'op.drop_column("users", "is_platform_admin")' in migration
    assert "ck_manual_recharge_orders_created_actor" in migration
    assert "ck_manual_recharge_events_actor" in migration
    assert "ck_admin_sessions_expiry_order" in migration
    assert "ForeignKey" not in migration


def test_admin_recovery_rls_migration_allows_only_explicit_system_updates() -> None:
    migration = Path(
        "migrations/versions/0018_admin_recovery_rls.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0017_physical_admin_identity"' in migration
    assert "admin_users_update_self" in migration
    assert "admin_sessions_update_scope" in migration
    assert migration.count("lumenx_current_admin_id() = 0") >= 4
    assert "ForeignKey" not in migration


def test_recharge_event_ledger_rls_allows_only_the_guarded_one_time_link() -> None:
    migration = Path(
        "migrations/versions/0019_recharge_event_ledger_rls.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0018_admin_recovery_rls"' in migration
    assert "manual_recharge_events_admin_link_ledger" in migration
    assert "FOR UPDATE" in migration
    assert "lumenx_is_platform_admin()" in migration
    assert "ledger_entry_id IS NULL" in migration
    assert "ledger_entry_id IS NOT NULL" in migration
    assert "ForeignKey" not in migration
