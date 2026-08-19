"""Preserve access to system media referenced by user scene copies."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0015_system_media_copy_access"
down_revision: str | None = "0014_expand_admin_console"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
                    WHERE assets.media_object_id = media_objects.id
                      AND assets.deleted_at IS NULL
                      AND (
                          (
                              assets.scope = 'system'
                              AND assets.asset_type = 'scene'
                              AND COALESCE(assets.payload->>'visibility', 'disabled') = 'enabled'
                          )
                          OR (
                              assets.scope <> 'system'
                              AND assets.user_id = lumenx_current_user_id()
                              AND assets.provenance->>'origin' = 'system_scene_copy'
                          )
                      )
                )
            )
        )
        WITH CHECK (
            (scope = 'user' AND user_id = lumenx_current_user_id())
            OR lumenx_is_platform_admin()
        )
        """
    )


def downgrade() -> None:
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
