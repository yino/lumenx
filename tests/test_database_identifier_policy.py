from __future__ import annotations

from pathlib import Path

from sqlalchemy import BigInteger, Integer, Uuid

from src.platform.db_models import Base


def test_all_application_tables_use_one_autoincrement_integer_id_primary_key() -> None:
    assert Base.metadata.tables
    for table in Base.metadata.sorted_tables:
        primary_key_columns = list(table.primary_key.columns)
        assert [column.name for column in primary_key_columns] == ["id"], table.name
        identifier = primary_key_columns[0]
        assert isinstance(identifier.type, (BigInteger, Integer)), table.name
        assert identifier.autoincrement is True, table.name


def test_application_metadata_has_no_uuid_columns_or_foreign_keys() -> None:
    foreign_keys = [
        str(foreign_key)
        for table in Base.metadata.sorted_tables
        for foreign_key in table.foreign_keys
    ]
    assert foreign_keys == []
    uuid_columns = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, Uuid)
    ]
    assert uuid_columns == []


def test_migrations_create_only_identity_id_primary_keys_without_foreign_keys() -> None:
    for migration in sorted(Path("migrations/versions").glob("*.py")):
        source = migration.read_text(encoding="utf-8")
        create_table_count = source.count("op.create_table(")
        assert source.count('sa.PrimaryKeyConstraint("id"') == create_table_count, migration.name
        assert source.count("sa.Identity()") == create_table_count, migration.name
        forbidden_tokens = ["ForeignKey", "REFERENCES"]
        if migration.name != "0010_integer_ids_without_foreign_keys.py":
            forbidden_tokens.extend(
                ["postgresql.UUID", "gen_random_uuid", "::uuid"]
            )
        for forbidden in forbidden_tokens:
            assert forbidden not in source, f"{migration.name}: {forbidden}"
