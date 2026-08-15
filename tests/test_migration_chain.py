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
