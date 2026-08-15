"""Bootstrap a fail-closed runtime policy when no active version exists."""

from __future__ import annotations

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa


revision: str = "0011_safe_runtime_policy"
down_revision: str | None = "0010_integer_ids_no_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BOOTSTRAP_REASON = "系统初始化安全运行策略"


def upgrade() -> None:
    connection = op.get_bind()
    active_id = connection.scalar(
        sa.text("SELECT id FROM config_versions WHERE status = 'active' LIMIT 1")
    )
    if active_id is not None:
        return

    version_number = connection.scalar(
        sa.text("SELECT COALESCE(MAX(version_number), 0) + 1 FROM config_versions")
    )
    config_id = connection.scalar(
        sa.text(
            """
            INSERT INTO config_versions
                (version_number, status, schema_version, created_by_user_id,
                 reason, activated_at)
            VALUES
                (:version_number, 'active', 1, 0, :reason, now())
            RETURNING id
            """
        ),
        {"version_number": version_number, "reason": _BOOTSTRAP_REASON},
    )
    settings = {
        "exposed_capabilities": [],
        "feature_flags": {
            "registration_mode": "disabled",
            "new_ai_tasks_enabled": False,
        },
        "operational": {
            "signed_media_url_seconds": 300,
            "soft_delete_retention_days": 30,
            "stale_hold_minutes": 30,
        },
    }
    connection.execute(
        sa.text(
            """
            INSERT INTO platform_configs
                (config_version_id, tokens_per_ticket,
                 registration_initial_grant_microtickets,
                 session_idle_seconds, session_absolute_seconds,
                 max_sessions_per_user, max_ai_concurrency_per_user, settings)
            VALUES
                (:config_id, 1000, 0, 604800, 2592000, 5, 2,
                 CAST(:settings AS jsonb))
            """
        ),
        {"config_id": config_id, "settings": json.dumps(settings)},
    )


def downgrade() -> None:
    connection = op.get_bind()
    config_id = connection.scalar(
        sa.text(
            """
            SELECT id
            FROM config_versions
            WHERE created_by_user_id = 0 AND reason = :reason
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"reason": _BOOTSTRAP_REASON},
    )
    if config_id is None:
        return
    op.execute("ALTER TABLE platform_configs DISABLE TRIGGER platform_configs_immutable")
    op.execute("ALTER TABLE model_configs DISABLE TRIGGER model_configs_immutable")
    op.execute("ALTER TABLE config_versions DISABLE TRIGGER config_versions_content_immutable")
    connection.execute(
        sa.text("DELETE FROM platform_configs WHERE config_version_id = :config_id"),
        {"config_id": config_id},
    )
    connection.execute(
        sa.text("DELETE FROM model_configs WHERE config_version_id = :config_id"),
        {"config_id": config_id},
    )
    connection.execute(
        sa.text("DELETE FROM config_versions WHERE id = :config_id"),
        {"config_id": config_id},
    )
    op.execute("ALTER TABLE config_versions ENABLE TRIGGER config_versions_content_immutable")
    op.execute("ALTER TABLE model_configs ENABLE TRIGGER model_configs_immutable")
    op.execute("ALTER TABLE platform_configs ENABLE TRIGGER platform_configs_immutable")
