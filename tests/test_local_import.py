from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.apps.comic_gen.models import Character, GlobalAssetLibrary, Script, Series
from src.apps.playground.models import PlaygroundGeneration, PlaygroundMode, PlaygroundOutput
from src.platform.contracts import UserContext
from src.platform.db_models import (
    AITaskRecord,
    AuditEventRecord,
    ImportBatchItemRecord,
    ImportBatchRecord,
    ImportedPlaygroundHistoryRecord,
    MediaObjectRecord,
    ProjectRecord,
    SeriesRecord,
)
from src.platform.local_import import (
    LocalImportConflictError,
    LocalImportService,
    LocalImportValidationError,
    LocalSourceDiscovery,
)
from src.platform.media_storage import CloudMediaStorage
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_media_storage import FakePrivateObjectStore


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _source_tree(root: Path, *, missing_media: bool = False) -> tuple[Series, Script]:
    output = root / "source" / "output"
    series = Series(
        id=str(uuid.uuid4()),
        title="第一季",
        episode_ids=[],
        created_at=1.0,
        updated_at=1.0,
    )
    project = Script(
        id=str(uuid.uuid4()),
        title="第一集",
        original_text="故事正文",
        series_id=series.id,
        characters=[
            Character(
                id="hero",
                name="主角",
                description="主角描述",
                image_url="/files/assets/hero.png",
            )
        ],
        created_at=1.0,
        updated_at=1.0,
    )
    series = series.model_copy(update={"episode_ids": [project.id]})
    _write_json(output / "series.json", {series.id: series.model_dump(mode="json")})
    _write_json(output / "projects.json", {project.id: project.model_dump(mode="json")})
    _write_json(
        output / "library_assets.json",
        GlobalAssetLibrary(
            characters=[Character(id="library-hero", name="资产库角色", description="资产库角色描述")]
        ).model_dump(mode="json"),
    )
    generation = PlaygroundGeneration(
        id=str(uuid.uuid4()),
        mode=PlaygroundMode.T2I,
        model_id="wan-image",
        prompt="角色立绘",
        outputs=[
            PlaygroundOutput(
                id=str(uuid.uuid4()),
                media_path="playground/result.png",
                media_type="image",
            )
        ],
        status="completed",
        created_at="2026-08-14T00:00:00Z",
    )
    _write_json(output / "playground_history.json", [generation.model_dump(mode="json")])
    if not missing_media:
        (output / "assets").mkdir(parents=True, exist_ok=True)
        (output / "assets" / "hero.png").write_bytes(b"hero-image")
        (output / "playground").mkdir(parents=True, exist_ok=True)
        (output / "playground" / "result.png").write_bytes(b"playground-image")
    return series, project


def _imported_target_id(
    session,
    batch_id: str,
    item_type: str,
    source_id: str,
) -> int:
    target_id = session.scalar(
        select(ImportBatchItemRecord.target_id).where(
            ImportBatchItemRecord.batch_id == int(batch_id),
            ImportBatchItemRecord.item_type == item_type,
            ImportBatchItemRecord.source_key == f"{item_type}:{source_id}",
        )
    )
    assert target_id is not None
    return int(target_id)


@pytest.fixture
def import_environment(tmp_path: Path):
    database = RepositoryDatabase()
    ImportBatchRecord.__table__.create(database.engine)
    ImportBatchItemRecord.__table__.create(database.engine)
    ImportedPlaygroundHistoryRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    AITaskRecord.__table__.create(database.engine)
    context = _create_scope(database)
    admin = replace(context.identity, is_platform_admin=True)
    object_store = FakePrivateObjectStore()
    storage = CloudMediaStorage(database, object_store)
    service = LocalImportService(
        database,
        storage,
        allowed_root=tmp_path,
        max_import_bytes=1024 * 1024,
    )
    yield database, context, admin, object_store, service
    database.engine.dispose()


def test_dry_run_reports_missing_media_without_writing_business_data(
    tmp_path: Path,
    import_environment,
) -> None:
    database, context, admin, _object_store, service = import_environment
    _source_tree(tmp_path, missing_media=True)

    report = service.dry_run(
        admin,
        target_user_id=context.identity.user_id,
        target_workspace_id=context.workspace_id,
        source_directory="source",
        missing_media_policy="reject",
    )

    assert report["ready"] is False
    assert any(issue["code"] == "MEDIA_REFERENCE_MISSING" for issue in report["issues"])
    with database.session_factory() as session:
        assert session.scalar(select(func.count(ProjectRecord.id))) == 0
        assert session.scalar(select(func.count(SeriesRecord.id))) == 0
        assert session.scalar(select(func.count(MediaObjectRecord.id))) == 0


def test_import_rejects_target_user_workspace_mismatch(tmp_path: Path, import_environment) -> None:
    database, context, admin, _object_store, service = import_environment
    _source_tree(tmp_path)
    foreign = _create_scope(database)

    with pytest.raises(LocalImportValidationError, match="归属不匹配"):
        service.dry_run(
            admin,
            target_user_id=context.identity.user_id,
            target_workspace_id=foreign.workspace_id,
            source_directory="source",
        )


def test_import_execution_is_idempotent_and_reconciles_integrity(
    tmp_path: Path,
    import_environment,
) -> None:
    database, context, admin, object_store, service = import_environment
    series, project = _source_tree(tmp_path)
    dry_run = service.dry_run(
        admin,
        target_user_id=context.identity.user_id,
        target_workspace_id=context.workspace_id,
        source_directory="source",
    )
    assert dry_run["ready"] is True

    first = service.execute(admin, dry_run["batch_id"])
    second = service.execute(admin, dry_run["batch_id"])

    assert first["status"] == "completed"
    assert first["integrity_ok"] is True
    assert second["status"] == "completed"
    assert second["actual_counts"] == first["actual_counts"]
    with database.session_factory() as session:
        assert session.scalar(select(func.count(ProjectRecord.id))) == 1
        assert session.scalar(select(func.count(SeriesRecord.id))) == 1
        assert session.scalar(select(func.count(MediaObjectRecord.id))) == 2
        assert session.scalar(select(func.count(ImportedPlaygroundHistoryRecord.id))) == 1
        stored_project = session.get(
            ProjectRecord,
            _imported_target_id(session, dry_run["batch_id"], "project", project.id),
        )
        stored_series = session.get(
            SeriesRecord,
            _imported_target_id(session, dry_run["batch_id"], "series", series.id),
        )
        assert stored_project.user_id == int(context.identity.user_id)
        assert stored_project.workspace_id == int(context.workspace_id)
        assert stored_project.series_id == stored_series.id
        assert stored_project.payload["characters"][0]["image_url"].startswith("media:")
    assert len(object_store.objects) == 2


def test_partial_media_failure_resumes_without_duplicate_records(
    tmp_path: Path,
    import_environment,
) -> None:
    database, context, admin, object_store, service = import_environment
    _source_tree(tmp_path)
    dry_run = service.dry_run(
        admin,
        target_user_id=context.identity.user_id,
        target_workspace_id=context.workspace_id,
        source_directory="source",
    )
    object_store.fail_put = True

    failed = service.execute(admin, dry_run["batch_id"])
    assert failed["status"] == "failed"
    object_store.fail_put = False
    resumed = service.execute(admin, dry_run["batch_id"])

    assert resumed["status"] == "completed"
    assert resumed["integrity_ok"] is True
    with database.session_factory() as session:
        assert session.scalar(select(func.count(ProjectRecord.id))) == 1
        assert session.scalar(select(func.count(MediaObjectRecord.id))) == 2
        failed_items = session.scalar(
            select(func.count(ImportBatchItemRecord.id)).where(
                ImportBatchItemRecord.status == "failed"
            )
        )
        assert failed_items == 0


def test_source_change_after_dry_run_requires_new_batch(tmp_path: Path, import_environment) -> None:
    _database, context, admin, _object_store, service = import_environment
    _source_tree(tmp_path)
    dry_run = service.dry_run(
        admin,
        target_user_id=context.identity.user_id,
        target_workspace_id=context.workspace_id,
        source_directory="source",
    )
    (tmp_path / "source" / "output" / "assets" / "hero.png").write_bytes(b"changed")

    with pytest.raises(LocalImportConflictError, match="发生变化"):
        service.execute(admin, dry_run["batch_id"])


def test_rollback_preserves_reused_and_subsequently_referenced_media(
    tmp_path: Path,
    import_environment,
) -> None:
    database, context, admin, _object_store, service = import_environment
    _series, project = _source_tree(tmp_path)
    dry_run = service.dry_run(
        admin,
        target_user_id=context.identity.user_id,
        target_workspace_id=context.workspace_id,
        source_directory="source",
    )
    service.execute(admin, dry_run["batch_id"])
    with database.session_factory.begin() as session:
        project_target_id = _imported_target_id(
            session,
            dry_run["batch_id"],
            "project",
            project.id,
        )
        project_record = session.get(ProjectRecord, project_target_id)
        project_record.version = 2

    result = service.rollback(admin, dry_run["batch_id"], "测试安全回滚")

    assert result["preserved_items"] >= 1
    with database.session_factory() as session:
        project_record = session.get(ProjectRecord, project_target_id)
        assert project_record.deleted_at is None
        active_media = list(
            session.scalars(
                select(MediaObjectRecord).where(MediaObjectRecord.lifecycle_state == "active")
            )
        )
        assert active_media
        batch = session.get(ImportBatchRecord, int(dry_run["batch_id"]))
        assert batch.status == "reverted"
        assert batch.rollback_reason == "测试安全回滚"


def test_discovery_rejects_paths_outside_allowed_root(tmp_path: Path) -> None:
    discovery = LocalSourceDiscovery(tmp_path, max_import_bytes=1024)
    with pytest.raises(LocalImportValidationError, match="允许范围"):
        discovery.discover(
            "../outside",
            include_playground=False,
            missing_media_policy="reject",
        )
