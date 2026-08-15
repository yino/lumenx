from __future__ import annotations

import uuid

import pytest

from src.apps.comic_gen.models import Character
from src.platform.asset_repositories import (
    AssetConflictError,
    AssetResolver,
    AssetScope,
    PostgresProjectAssetRepository,
    PostgresSeriesAssetRepository,
    PostgresWorkspaceAssetRepository,
    ReadOnlySystemAssetRepository,
)
from src.platform.content_repositories import (
    OptimisticVersionConflictError,
    ScopedDocumentNotFoundError,
)
from src.platform.content_service import CloudContentService
from src.platform.db_models import AssetRecord
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor


@pytest.fixture
def asset_environment():
    database = RepositoryDatabase()
    context = _create_scope(database)
    content = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    )
    series = content.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    project = content.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
        series_id=series.document.id,
    )
    yield database, context, series.document.id, project.document.id
    database.engine.dispose()


def _character(name: str, *, asset_id: str = "hero") -> Character:
    return Character(id=asset_id, name=name, description=f"{name}描述")


def test_asset_resolver_uses_project_series_workspace_system_priority(
    asset_environment,
) -> None:
    database, context, series_id, project_id = asset_environment
    project_repository = PostgresProjectAssetRepository(database)
    series_repository = PostgresSeriesAssetRepository(database)
    workspace_repository = PostgresWorkspaceAssetRepository(database)
    resolver = AssetResolver(database)

    workspace_asset = workspace_repository.add(
        context,
        None,
        "character",
        _character("工作区角色"),
    )
    series_asset = series_repository.add(
        context,
        series_id,
        "character",
        _character("系列角色"),
    )
    project_asset = project_repository.add(
        context,
        project_id,
        "character",
        _character("项目角色"),
    )
    system_character = _character("系统角色")
    with database.session_factory.begin() as session:
        system_record = AssetRecord(
                user_id=None,
                workspace_id=None,
                project_id=None,
                series_id=None,
                media_object_id=None,
                scope="system",
                asset_type="character",
                name=system_character.name,
                payload=system_character.model_dump(mode="json"),
                provenance={"origin": "platform"},
                schema_version=1,
                version=1,
            )
        session.add(system_record)
        session.flush()
        system_record_id = system_record.id

    resolved = resolver.resolve(
        context,
        "character",
        "hero",
        project_id=project_id,
    )
    assert resolved is not None
    assert resolved.scope is AssetScope.PROJECT

    project_repository.soft_delete(
        context,
        project_id,
        project_asset.record_id,
        project_asset.version,
    )
    assert resolver.resolve(
        context,
        "character",
        "hero",
        project_id=project_id,
    ).scope is AssetScope.SERIES

    series_repository.soft_delete(
        context,
        series_id,
        series_asset.record_id,
        series_asset.version,
    )
    assert resolver.resolve(
        context,
        "character",
        "hero",
        project_id=project_id,
    ).scope is AssetScope.WORKSPACE

    workspace_repository.soft_delete(
        context,
        None,
        workspace_asset.record_id,
        workspace_asset.version,
    )
    resolved_system = resolver.resolve(
        context,
        "character",
        "hero",
        project_id=project_id,
    )
    assert resolved_system is not None
    assert resolved_system.record_id == str(system_record_id)
    assert resolved_system.scope is AssetScope.SYSTEM


def test_asset_provenance_derives_ownership_from_context(asset_environment) -> None:
    database, context, _series_id, project_id = asset_environment
    repository = PostgresProjectAssetRepository(database)

    stored = repository.add(
        context,
        project_id,
        "character",
        _character("主角"),
        provenance={
            "origin": "promoted",
            "source_asset_record_id": str(uuid.uuid4()),
            "workspace_id": "forged-workspace",
        },
    )

    assert stored.provenance["origin"] == "promoted"
    assert stored.provenance["created_by_user_id"] == context.identity.user_id
    assert stored.provenance["workspace_id"] == context.workspace_id
    assert stored.provenance["project_id"] == project_id
    assert "series_id" not in stored.provenance


def test_asset_repository_hides_foreign_parent(asset_environment) -> None:
    database, owner_context, _series_id, project_id = asset_environment
    foreign_context = _create_scope(database)
    repository = PostgresProjectAssetRepository(database)
    stored = repository.add(
        owner_context,
        project_id,
        "character",
        _character("主角"),
    )

    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        repository.list(foreign_context, project_id)
    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        repository.get(foreign_context, project_id, stored.record_id)


def test_asset_updates_enforce_optimistic_versions(asset_environment) -> None:
    database, context, _series_id, project_id = asset_environment
    repository = PostgresProjectAssetRepository(database)
    stored = repository.add(
        context,
        project_id,
        "character",
        _character("旧名称"),
    )

    updated = repository.update(
        context,
        project_id,
        stored.record_id,
        _character("新名称"),
        expected_version=1,
    )

    assert updated.version == 2
    assert updated.name == "新名称"
    with pytest.raises(OptimisticVersionConflictError, match="刷新后重试"):
        repository.update(
            context,
            project_id,
            stored.record_id,
            _character("过期名称"),
            expected_version=1,
        )


def test_duplicate_domain_id_is_rejected_within_same_scope(asset_environment) -> None:
    database, context, _series_id, project_id = asset_environment
    repository = PostgresProjectAssetRepository(database)
    repository.add(
        context,
        project_id,
        "character",
        _character("主角"),
    )

    with pytest.raises(AssetConflictError, match="已存在"):
        repository.add(
            context,
            project_id,
            "character",
            _character("同标识角色"),
        )


def test_system_asset_repository_exposes_no_mutation_methods(asset_environment) -> None:
    database, context, _series_id, _project_id = asset_environment
    repository = ReadOnlySystemAssetRepository(database)

    assert repository.list(context) == []
    assert not hasattr(repository, "add")
    assert not hasattr(repository, "update")
    assert not hasattr(repository, "soft_delete")
