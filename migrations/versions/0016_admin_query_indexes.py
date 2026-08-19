"""Add indexes for bounded administrator queries."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0016_admin_query_indexes"
down_revision: str | None = "0015_system_media_copy_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_users_admin_created",
        "users",
        ["created_at", "id"],
    )
    op.create_index(
        "ix_ai_tasks_admin_created",
        "ai_tasks",
        ["created_at", "id"],
    )
    op.create_index(
        "ix_ai_tasks_admin_owner_created",
        "ai_tasks",
        ["user_id", "workspace_id", "created_at", "id"],
    )
    op.create_index(
        "ix_usage_events_admin_created",
        "usage_events",
        ["created_at", "id"],
    )
    op.create_index(
        "ix_assets_admin_owner_updated",
        "assets",
        ["user_id", "workspace_id", "asset_type", "updated_at", "id"],
    )
    op.create_index(
        "ix_media_objects_admin_owner_created",
        "media_objects",
        ["user_id", "workspace_id", "created_at", "id"],
    )
    op.create_index(
        "ix_manual_recharge_orders_status_created",
        "manual_recharge_orders",
        ["status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_manual_recharge_orders_status_created",
        table_name="manual_recharge_orders",
    )
    op.drop_index(
        "ix_media_objects_admin_owner_created",
        table_name="media_objects",
    )
    op.drop_index("ix_assets_admin_owner_updated", table_name="assets")
    op.drop_index("ix_usage_events_admin_created", table_name="usage_events")
    op.drop_index("ix_ai_tasks_admin_owner_created", table_name="ai_tasks")
    op.drop_index("ix_ai_tasks_admin_created", table_name="ai_tasks")
    op.drop_index("ix_users_admin_created", table_name="users")
