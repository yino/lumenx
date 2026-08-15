from __future__ import annotations

import pytest

from src.apps.comic_gen.models import AssetUnit, Character, ImageVariant
from src.platform.asset_repositories import AssetScope
from src.platform.asset_service import (
    AssetMutationForbiddenError,
    CloudAssetService,
)
from src.platform.content_repositories import ScopedDocumentNotFoundError
from src.platform.content_service import CloudContentService
from src.platform.db_models import AssetRecord
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor


@pytest.fixture
def asset_service_environment():
    database = RepositoryDatabase()
    context = _create_scope(database)
    content = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    )
    source_series = content.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    target_series = content.create_series(
        context,
        title="第二季",
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
        series_id=source_series.document.id,
    )
    service = CloudAssetService(database)
    yield (
        database,
        context,
        service,
        source_series.document.id,
        target_series.document.id,
        project.document.id,
    )
    database.engine.dispose()


def test_asset_service_crud_across_owned_scopes(asset_service_environment) -> None:
    (
        _database,
        context,
        service,
        series_id,
        _target_series_id,
        project_id,
    ) = asset_service_environment

    project_asset = service.create_project_asset(
        context,
        project_id,
        "character",
        name="林墨",
        description="主角",
        age="22",
    )
    series_asset = service.create_series_asset(
        context,
        series_id,
        "scene",
        name="旧车站",
        description="雨夜车站",
        time_of_day="夜晚",
    )
    library_asset = service.create_library_asset(
        context,
        "prop",
        name="怀表",
        description="铜制怀表",
    )

    assert project_asset.document.age == "22"
    assert service.list_project_assets(context, project_id)[0].domain_id == (
        project_asset.domain_id
    )
    assert service.list_series_assets(context, series_id)[0].domain_id == (
        series_asset.domain_id
    )
    assert service.list_library_assets(context)[0].domain_id == library_asset.domain_id

    updated = service.mutate_library_attributes(
        context,
        "prop",
        library_asset.domain_id,
        {"description": "修复后的铜制怀表"},
        library_asset.version,
    )
    assert updated.document.description == "修复后的铜制怀表"
    service.delete_project_asset(
        context,
        project_id,
        "character",
        project_asset.domain_id,
        project_asset.version,
    )
    service.delete_series_asset(
        context,
        series_id,
        "scene",
        series_asset.domain_id,
        series_asset.version,
    )
    service.delete_library_asset(
        context,
        "prop",
        library_asset.domain_id,
        updated.version,
    )
    assert service.list_project_assets(context, project_id) == []
    assert service.list_series_assets(context, series_id) == []
    assert service.list_library_assets(context) == []


def test_project_mutation_respects_resolved_scope(asset_service_environment) -> None:
    (
        database,
        context,
        service,
        series_id,
        _target_series_id,
        project_id,
    ) = asset_service_environment
    inherited = service.create_series_asset(
        context,
        series_id,
        "character",
        name="系列角色",
        description="共享角色",
    )
    workspace_asset = service.create_library_asset(
        context,
        "character",
        name="工作区角色",
        description="工作区共享角色",
    )
    system_character = Character(
        id="system-hero",
        name="系统角色",
        description="平台只读角色",
    )
    with database.session_factory.begin() as session:
        session.add(
            AssetRecord(
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
        )

    updated = service.toggle_project_field(
        context,
        project_id,
        "character",
        inherited.domain_id,
        "starred",
        inherited.version,
    )
    assert updated.scope is AssetScope.SERIES
    assert updated.document.starred is True
    effective = {
        asset.domain_id: asset.scope
        for asset in service.list_project_assets(context, project_id)
    }
    assert effective[inherited.domain_id] is AssetScope.SERIES
    assert effective[workspace_asset.domain_id] is AssetScope.WORKSPACE
    assert effective[system_character.id] is AssetScope.SYSTEM

    with pytest.raises(AssetMutationForbiddenError, match="先复制"):
        service.toggle_project_field(
            context,
            project_id,
            "character",
            workspace_asset.domain_id,
            "starred",
            workspace_asset.version,
        )
    with pytest.raises(AssetMutationForbiddenError, match="先复制"):
        service.toggle_project_field(
            context,
            project_id,
            "character",
            system_character.id,
            "starred",
            1,
        )
    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        service.mutate_library_attributes(
            context,
            "character",
            "missing",
            {"name": "不存在"},
            1,
        )


def test_promotion_fork_and_series_import_record_provenance(
    asset_service_environment,
) -> None:
    (
        _database,
        context,
        service,
        source_series_id,
        target_series_id,
        project_id,
    ) = asset_service_environment
    project_asset = service.create_project_asset(
        context,
        project_id,
        "character",
        name="项目角色",
        description="项目私有角色",
    )
    promoted = service.promote_to_library(
        context,
        source_kind="project",
        source_id=project_id,
        asset_type="character",
        domain_id=project_asset.domain_id,
    )
    forked = service.fork_to_project(
        context,
        project_id,
        "character",
        promoted.domain_id,
    )
    source_series_asset = service.create_series_asset(
        context,
        source_series_id,
        "prop",
        name="钥匙",
        description="系列道具",
    )
    imported, skipped = service.import_series_assets(
        context,
        target_series_id,
        source_series_id,
        [source_series_asset.domain_id, "missing"],
    )

    assert promoted.domain_id != project_asset.domain_id
    assert promoted.provenance["origin"] == "promoted"
    assert promoted.provenance["source_asset_record_id"] == project_asset.record_id
    assert forked.domain_id != promoted.domain_id
    assert forked.provenance["origin"] == "forked"
    assert len(imported) == 1
    assert imported[0].domain_id != source_series_asset.domain_id
    assert imported[0].provenance["origin"] == "imported"
    assert skipped == ["missing"]


def test_custom_voice_binding_and_voice_parameters(asset_service_environment) -> None:
    (
        _database,
        context,
        service,
        series_id,
        _target_series_id,
        project_id,
    ) = asset_service_environment
    character = service.create_project_asset(
        context,
        project_id,
        "character",
        name="林墨",
        description="主角",
    )
    voice = service.add_custom_voice(
        context,
        series_id,
        voice_id="voice-owned",
        label="林墨音色",
        origin="design",
        target_model="cosyvoice-v3.5-plus",
        voice_prompt="冷静的青年男声",
    )

    bound = service.bind_character_voice(
        context,
        project_id,
        character.domain_id,
        voice.document.id,
        voice.document.label,
        character.version,
    )
    tuned = service.update_character_voice_params(
        context,
        project_id,
        character.domain_id,
        speed=1.1,
        pitch=0.9,
        volume=60,
        expected_version=bound.version,
    )

    assert service.list_custom_voices(context, series_id)[0].domain_id == "voice-owned"
    assert tuned.document.voice_id == "voice-owned"
    assert tuned.document.voice_speed == 1.1
    assert tuned.document.voice_pitch == 0.9
    assert tuned.document.voice_volume == 60
    service.delete_custom_voice(
        context,
        series_id,
        "voice-owned",
        voice.version,
    )
    assert service.list_custom_voices(context, series_id) == []


def test_project_variant_selection_favorite_and_delete(asset_service_environment) -> None:
    (
        _database,
        context,
        service,
        _series_id,
        _target_series_id,
        project_id,
    ) = asset_service_environment
    character = Character(
        id="hero-with-variants",
        name="林墨",
        description="主角",
        image_url="media:first",
        reference_sheet=AssetUnit(
            selected_image_id="variant-first",
            image_variants=[
                ImageVariant(id="variant-first", url="media:first"),
                ImageVariant(id="variant-second", url="media:second"),
            ],
        ),
    )
    stored = service.project_assets.add(
        context,
        project_id,
        "character",
        character,
    )

    selected = service.select_project_variant(
        context,
        project_id,
        "character",
        character.id,
        "variant-second",
        "reference_sheet",
        stored.version,
    )
    favorited = service.favorite_project_variant(
        context,
        project_id,
        "character",
        character.id,
        "variant-second",
        "reference_sheet",
        True,
        selected.version,
    )
    deleted = service.delete_project_variant(
        context,
        project_id,
        "character",
        character.id,
        "variant-second",
        favorited.version,
    )

    assert selected.document.image_url == "media:second"
    assert favorited.document.reference_sheet.image_variants[1].is_favorited is True
    assert deleted.document.reference_sheet.selected_image_id == "variant-first"
    assert deleted.document.image_url == "media:first"
