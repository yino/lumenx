"""Create scoped content, asset, and media storage tables."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002_content_storage"
down_revision: str | None = "0001_identity_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _document_columns() -> list[sa.Column]:
    return [
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "series",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        *_document_columns(),
        sa.CheckConstraint("schema_version > 0", name="ck_series_schema_version_positive"),
        sa.CheckConstraint("version > 0", name="ck_series_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_series"),
        sa.UniqueConstraint("user_id", "workspace_id", "id", name="uq_series_owner_id"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("series_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        *_document_columns(),
        sa.CheckConstraint("schema_version > 0", name="ck_projects_schema_version_positive"),
        sa.CheckConstraint("version > 0", name="ck_projects_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("user_id", "workspace_id", "id", name="uq_projects_owner_id"),
    )
    op.create_table(
        "media_objects",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes >= 0", name="ck_media_objects_size_nonnegative"),
        sa.CheckConstraint("lifecycle_state IN ('pending', 'active', 'deleted', 'failed')", name="ck_media_objects_lifecycle"),
        sa.PrimaryKeyConstraint("id", name="pk_media_objects"),
        sa.UniqueConstraint("object_key", name="uq_media_objects_object_key"),
        sa.UniqueConstraint("user_id", "workspace_id", "id", name="uq_media_objects_owner_id"),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=True),
        sa.Column("project_id", sa.BigInteger(), nullable=True),
        sa.Column("series_id", sa.BigInteger(), nullable=True),
        sa.Column("media_object_id", sa.BigInteger(), nullable=True),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scope IN ('project', 'series', 'workspace', 'system')", name="ck_assets_scope"),
        sa.CheckConstraint("asset_type IN ('character', 'scene', 'prop', 'voice', 'other')", name="ck_assets_type"),
        sa.CheckConstraint("schema_version > 0 AND version > 0", name="ck_assets_versions_positive"),
        sa.CheckConstraint(
            "(scope = 'system' AND user_id IS NULL AND workspace_id IS NULL) OR "
            "(scope <> 'system' AND user_id IS NOT NULL AND workspace_id IS NOT NULL)",
            name="ck_assets_scope_owner",
        ),
        sa.CheckConstraint(
            "(scope = 'project' AND project_id IS NOT NULL AND series_id IS NULL) OR "
            "(scope = 'series' AND series_id IS NOT NULL AND project_id IS NULL) OR "
            "(scope IN ('workspace', 'system') AND project_id IS NULL AND series_id IS NULL)",
            name="ck_assets_scope_parent",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assets"),
        sa.UniqueConstraint("user_id", "workspace_id", "id", name="uq_assets_owner_id"),
    )


def downgrade() -> None:
    op.drop_table("assets")
    op.drop_table("media_objects")
    op.drop_table("projects")
    op.drop_table("series")
