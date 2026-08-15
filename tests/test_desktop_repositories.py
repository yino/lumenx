from __future__ import annotations

import json
import time

import pytest

from src.apps.comic_gen.models import GlobalAssetLibrary, Script, Series
from src.apps.playground.models import PlaygroundGeneration, PlaygroundMode, PlaygroundTemplate
from src.apps.playground.storage import DesktopPlaygroundRepository, PlaygroundStorage
from src.platform.contracts import (
    AggregateRepository,
    PlaygroundRepository,
    ScopedRepository,
    UserContext,
    WorkspaceContext,
)
from src.platform.desktop_repositories import (
    DesktopAssetLibraryRepository,
    DesktopJsonRepositoryError,
    DesktopProjectRepository,
    DesktopSeriesRepository,
)


def _context() -> WorkspaceContext:
    return WorkspaceContext(
        identity=UserContext(user_id="desktop-user"),
        workspace_id="desktop-workspace",
    )


def _script(resource_id: str = "project-1", title: str = "项目一") -> Script:
    now = time.time()
    return Script(
        id=resource_id,
        title=title,
        original_text="剧本文本",
        created_at=now,
        updated_at=now,
    )


def _series(resource_id: str = "series-1") -> Series:
    now = time.time()
    return Series(id=resource_id, title="系列一", created_at=now, updated_at=now)


def test_desktop_document_adapters_satisfy_scoped_repository_contract(tmp_path) -> None:
    projects = DesktopProjectRepository(tmp_path / "projects.json")
    series = DesktopSeriesRepository(tmp_path / "series.json")

    assert isinstance(projects, ScopedRepository)
    assert isinstance(series, ScopedRepository)

    stored_project = projects.add(_context(), _script())
    stored_series = series.add(_context(), _series())

    assert stored_project.version == 1
    assert stored_series.document.title == "系列一"
    assert projects.get(_context(), "project-1").document.title == "项目一"


def test_desktop_document_adapter_preserves_versions_and_legacy_json(tmp_path) -> None:
    path = tmp_path / "projects.json"
    repository = DesktopProjectRepository(path)
    repository.add(_context(), _script())

    updated = repository.update(
        _context(),
        "project-1",
        _script(title="项目二"),
        expected_version=1,
    )

    assert updated.version == 2
    assert json.loads(path.read_text(encoding="utf-8"))["project-1"]["title"] == "项目二"
    with pytest.raises(DesktopJsonRepositoryError, match="内容已更新"):
        repository.update(
            _context(),
            "project-1",
            _script(title="过期修改"),
            expected_version=1,
        )


def test_desktop_asset_library_adapter_satisfies_aggregate_contract(tmp_path) -> None:
    repository = DesktopAssetLibraryRepository(tmp_path / "library_assets.json")
    library = GlobalAssetLibrary()

    assert isinstance(repository, AggregateRepository)
    repository.save(library)

    assert repository.load() == library


def test_desktop_playground_adapter_satisfies_contract_and_keeps_alias(tmp_path) -> None:
    repository = DesktopPlaygroundRepository()
    repository.HISTORY_PATH = str(tmp_path / "playground_history.json")
    repository.TEMPLATES_PATH = str(tmp_path / "playground_templates.json")
    repository._load()
    generation = PlaygroundGeneration(
        id="generation-1",
        mode=PlaygroundMode.T2I,
        model_id="wanx-v1",
        prompt="霓虹城市",
        created_at="2026-08-13T00:00:00+00:00",
    )
    template = PlaygroundTemplate(
        id="template-1",
        name="城市模板",
        prompt="霓虹城市",
        created_at="2026-08-13T00:00:00+00:00",
        updated_at="2026-08-13T00:00:00+00:00",
    )

    assert isinstance(repository, PlaygroundRepository)
    assert PlaygroundStorage is DesktopPlaygroundRepository
    repository.add_generation(generation)
    repository.add_template(template)

    reloaded = DesktopPlaygroundRepository()
    reloaded.HISTORY_PATH = repository.HISTORY_PATH
    reloaded.TEMPLATES_PATH = repository.TEMPLATES_PATH
    reloaded._load()
    assert reloaded.get_generation("generation-1") == generation
    assert reloaded.get_template("template-1") == template
