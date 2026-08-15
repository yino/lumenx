from __future__ import annotations

import uuid

import pytest

from src.apps.comic_gen.models import (
    ArtDirection,
    Character,
    PromptConfig,
    Prop,
    Scene,
    Script,
)
from src.platform.asset_repositories import PostgresProjectAssetRepository
from src.platform.content_repositories import OptimisticVersionConflictError
from src.platform.content_service import CloudContentService
from src.platform.db_models import ProjectRecord, SeriesRecord
from tests.test_content_repositories import RepositoryDatabase, _create_scope, _script


class FakeScriptProcessor:
    def create_draft_script(self, title: str, text: str) -> Script:
        return _script(title=title, original_text=text)

    def parse_novel(
        self,
        title: str,
        text: str,
        _custom_prompt: str = "",
    ) -> Script:
        return _script(title=title, original_text=text)

    def analyze_script_for_styles(
        self,
        script_text: str,
        custom_prompt: str,
    ) -> list[dict[str, str]]:
        return [{"script_text": script_text, "custom_prompt": custom_prompt}]


class ParsedAssetScriptProcessor(FakeScriptProcessor):
    def __init__(self) -> None:
        self.parse_count = 0

    def parse_novel(
        self,
        title: str,
        text: str,
        _custom_prompt: str = "",
    ) -> Script:
        self.parse_count += 1
        suffix = str(self.parse_count)
        return _script(
            title=title,
            original_text=text,
            characters=[
                Character(
                    id=f"character-{suffix}",
                    name=f"角色{suffix}",
                    description="角色描述",
                )
            ],
            scenes=[
                Scene(
                    id=f"scene-{suffix}",
                    name=f"场景{suffix}",
                    description="场景描述",
                )
            ],
            props=[
                Prop(
                    id=f"prop-{suffix}",
                    name=f"道具{suffix}",
                    description="道具描述",
                )
            ],
        )


@pytest.fixture
def content_service():
    database = RepositoryDatabase()
    service = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    )
    yield database, service
    database.engine.dispose()


def test_create_project_with_series_updates_both_documents_atomically(content_service) -> None:
    database, service = content_service
    context = _create_scope(database)
    series = service.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )

    project = service.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
        series_id=series.document.id,
    )

    stored_series = service.get_series(context, series.document.id)
    assert project.document.series_id == series.document.id
    assert project.document.episode_number == 1
    assert stored_series.document.episode_ids == [project.document.id]
    assert stored_series.version == 2


def test_episode_binding_moves_project_between_series(content_service) -> None:
    database, service = content_service
    context = _create_scope(database)
    first = service.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    second = service.create_series(
        context,
        title="第二季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    project = service.create_project(
        context,
        title="特别篇",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
        series_id=first.document.id,
    )

    moved = service.add_episode(
        context,
        second.document.id,
        project.document.id,
        episode_number=3,
        expected_series_version=second.version,
    )

    assert moved.document.episode_ids == [project.document.id]
    assert service.get_series(context, first.document.id).document.episode_ids == []
    stored_project = service.get_project(context, project.document.id)
    assert stored_project.document.series_id == second.document.id
    assert stored_project.document.episode_number == 3


def test_stale_series_update_is_rejected(content_service) -> None:
    database, service = content_service
    context = _create_scope(database)
    series = service.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    service.update_series(context, series.document.id, {"title": "新标题"}, series.version)

    with pytest.raises(OptimisticVersionConflictError, match="刷新后重试"):
        service.update_series(
            context,
            series.document.id,
            {"title": "旧页面标题"},
            series.version,
        )


def test_delete_series_detaches_projects_and_soft_deletes_series(content_service) -> None:
    database, service = content_service
    context = _create_scope(database)
    series = service.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    project = service.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
        series_id=series.document.id,
    )
    current_series = service.get_series(context, series.document.id)

    service.delete_series(context, series.document.id, current_series.version)

    assert service.series.get(context, series.document.id) is None
    stored_project = service.get_project(context, project.document.id)
    assert stored_project.document.series_id is None
    assert stored_project.document.episode_number is None
    with database.session_factory() as session:
        record = session.get(SeriesRecord, int(series.document.id))
        assert record is not None
        assert record.deleted_at is not None


def test_delete_project_removes_series_reference_and_keeps_retention(content_service) -> None:
    database, service = content_service
    context = _create_scope(database)
    series = service.create_series(
        context,
        title="第一季",
        description="",
        workflow_mode="r2v",
        content_mode="scripted",
        default_generation_mode="r2v",
    )
    project = service.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
        series_id=series.document.id,
    )

    service.delete_project(context, project.document.id, project.version)

    assert service.projects.get(context, project.document.id) is None
    assert service.get_series(context, series.document.id).document.episode_ids == []
    with database.session_factory() as session:
        record = session.get(ProjectRecord, int(project.document.id))
        assert record is not None
        assert record.retention_expires_at is not None


def test_prompt_and_art_direction_updates_use_repository_versions(content_service) -> None:
    database, service = content_service
    context = _create_scope(database)
    project = service.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
    )
    prompt_config = PromptConfig(style_analysis="自定义风格分析")

    prompt_update = service.update_project_prompt_config(
        context,
        project.document.id,
        prompt_config,
        project.version,
    )
    recommendations = service.analyze_project_art_direction(
        context,
        project.document.id,
        "测试剧本",
    )
    art_direction = ArtDirection(
        selected_style_id="cinematic",
        style_config={"name": "电影感"},
    )
    art_update = service.save_project_art_direction(
        context,
        project.document.id,
        art_direction,
        prompt_update.version,
    )

    assert recommendations == [
        {"script_text": "测试剧本", "custom_prompt": "自定义风格分析"}
    ]
    assert art_update.document.art_direction == art_direction
    cleared = service.clear_project_art_direction(
        context,
        project.document.id,
        art_update.version,
    )
    assert cleared.document.art_direction is None


def test_parsed_entities_are_stored_only_in_project_asset_records() -> None:
    database = RepositoryDatabase()
    context = _create_scope(database)
    processor = ParsedAssetScriptProcessor()
    service = CloudContentService(database, script_processor=processor)
    assets = PostgresProjectAssetRepository(database)

    created = service.create_project(
        context,
        title="第一集",
        text="初始正文",
        skip_analysis=False,
        workflow_mode="r2v",
    )

    assert created.document.characters == []
    assert created.document.scenes == []
    assert created.document.props == []
    first_assets = assets.list(context, created.document.id)
    assert {asset.asset_type for asset in first_assets} == {
        "character",
        "scene",
        "prop",
    }
    assert {asset.provenance["origin"] for asset in first_assets} == {
        "script_analysis"
    }

    reparsed = service.reparse_project(
        context,
        created.document.id,
        "更新正文",
        created.version,
    )

    assert reparsed.document.characters == []
    replacement_assets = assets.list(context, created.document.id)
    assert {asset.name for asset in replacement_assets} == {
        "角色2",
        "场景2",
        "道具2",
    }
    database.engine.dispose()
