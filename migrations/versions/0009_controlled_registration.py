"""Add controlled registration invitations and immutable session policy snapshots."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0009_controlled_registration"
down_revision: str | None = "0008_audit_event_insert_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_configs DISABLE TRIGGER platform_configs_immutable"
    )
    op.execute(
        """
        UPDATE platform_configs
        SET settings = jsonb_set(
            settings #- '{feature_flags,registration_enabled}',
            '{feature_flags,registration_mode}',
            to_jsonb(
                CASE
                    WHEN COALESCE((settings #>> '{feature_flags,registration_enabled}')::boolean, false)
                    THEN 'invite_only'::text
                    ELSE 'disabled'::text
                END
            ),
            true
        )
        WHERE settings #> '{feature_flags,registration_mode}' IS NULL
        """
    )
    op.execute(
        "UPDATE platform_configs SET settings = settings #- "
        "'{operational,global_worker_concurrency}'"
    )
    op.execute(
        "ALTER TABLE platform_configs ENABLE TRIGGER platform_configs_immutable"
    )
    op.add_column("auth_sessions", sa.Column("idle_timeout_seconds", sa.Integer()))
    op.add_column(
        "auth_sessions",
        sa.Column("configuration_version_id", sa.BigInteger()),
    )
    op.execute(
        "UPDATE auth_sessions SET idle_timeout_seconds = GREATEST(1, "
        "EXTRACT(EPOCH FROM (idle_expires_at - last_seen_at))::integer) "
        "WHERE idle_timeout_seconds IS NULL"
    )
    op.create_check_constraint(
        "ck_auth_sessions_idle_timeout_positive",
        "auth_sessions",
        "idle_timeout_seconds IS NULL OR idle_timeout_seconds > 0",
    )
    op.create_table(
        "registration_invitations",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("phone_canonical", sa.String(length=32), nullable=False),
        sa.Column("invitation_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_admin_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_by_user_id", sa.BigInteger()),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'consumed', 'revoked', 'expired')",
            name="ck_registration_invitations_status",
        ),
        sa.CheckConstraint(
            "(status = 'consumed' AND consumed_by_user_id IS NOT NULL AND consumed_at IS NOT NULL) "
            "OR (status <> 'consumed' AND consumed_by_user_id IS NULL AND consumed_at IS NULL)",
            name="ck_registration_invitations_consumption",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invitation_hash", name="uq_registration_invitations_hash"),
    )
    op.create_index(
        "ix_registration_invitations_phone_status_expiry",
        "registration_invitations",
        ["phone_canonical", "status", "expires_at"],
    )
    op.create_index(
        "ix_registration_invitations_status_created",
        "registration_invitations",
        ["status", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION lumenx_registration_invitation_hash() RETURNS text
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$
            SELECT NULLIF(current_setting('app.registration_invitation_hash', true), '')
        $$
        """
    )
    op.execute("ALTER TABLE registration_invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE registration_invitations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY registration_invitations_select_scope ON registration_invitations "
        "FOR SELECT USING (invitation_hash = lumenx_registration_invitation_hash() "
        "OR lumenx_is_platform_admin())"
    )
    op.execute(
        "CREATE POLICY registration_invitations_insert_admin ON registration_invitations "
        "FOR INSERT WITH CHECK (lumenx_is_platform_admin())"
    )
    op.execute(
        "CREATE POLICY registration_invitations_update_scope ON registration_invitations "
        "FOR UPDATE USING (invitation_hash = lumenx_registration_invitation_hash() "
        "OR lumenx_is_platform_admin()) WITH CHECK "
        "(invitation_hash = lumenx_registration_invitation_hash() OR lumenx_is_platform_admin())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS registration_invitations_update_scope ON registration_invitations")
    op.execute("DROP POLICY IF EXISTS registration_invitations_insert_admin ON registration_invitations")
    op.execute("DROP POLICY IF EXISTS registration_invitations_select_scope ON registration_invitations")
    op.execute("DROP FUNCTION IF EXISTS lumenx_registration_invitation_hash()")
    op.drop_index("ix_registration_invitations_status_created", table_name="registration_invitations")
    op.drop_index(
        "ix_registration_invitations_phone_status_expiry",
        table_name="registration_invitations",
    )
    op.drop_table("registration_invitations")
    op.drop_constraint(
        "ck_auth_sessions_idle_timeout_positive", "auth_sessions", type_="check"
    )
    op.drop_column("auth_sessions", "configuration_version_id")
    op.drop_column("auth_sessions", "idle_timeout_seconds")
