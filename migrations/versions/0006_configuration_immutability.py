"""Protect immutable configuration payloads."""

from collections.abc import Sequence

from alembic import op


revision: str = "0006_configuration_immutability"
down_revision: str | None = "0005_constraints_indexes_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_configuration_payload_mutation() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'configuration payloads are immutable';
        END;
        $$
        """
    )
    for table in ("model_configs", "platform_configs"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_configuration_payload_mutation()
            """
        )

    op.execute(
        """
        CREATE FUNCTION protect_config_version_content() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'configuration versions are immutable';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.version_number IS DISTINCT FROM OLD.version_number
                OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
                OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
                OR NEW.reason IS DISTINCT FROM OLD.reason
                OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'configuration version content is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER config_versions_content_immutable
        BEFORE UPDATE OR DELETE ON config_versions
        FOR EACH ROW EXECUTE FUNCTION protect_config_version_content()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS config_versions_content_immutable ON config_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_config_version_content()")
    for table in reversed(("model_configs", "platform_configs")):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_configuration_payload_mutation()")
