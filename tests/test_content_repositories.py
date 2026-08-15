from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.apps.comic_gen.models import Script, Series
from src.platform.content_repositories import (
    DocumentPayloadValidationError,
    InvalidRepositoryContextError,
    OptimisticVersionConflictError,
    PostgresProjectRepository,
    PostgresSeriesRepository,
    ScopedDocumentNotFoundError,
)
from src.platform.contracts import UserContext, WorkspaceContext
from src.platform.db_models import (
    AssetRecord,
    MediaObjectRecord,
    ProjectRecord,
    SeriesRecord,
    UserRecord,
    WorkspaceRecord,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json(_type, _compiler, **_kwargs):
    return "JSON"


class RepositoryDatabase:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=Session,
        )
        UserRecord.__table__.create(self.engine)
        WorkspaceRecord.__table__.create(self.engine)
        SeriesRecord.__table__.create(self.engine)
        ProjectRecord.__table__.create(self.engine)
        MediaObjectRecord.__table__.create(self.engine)
        AssetRecord.__table__.create(self.engine)
        self.identities: list[UserContext] = []

    @contextmanager
    def transaction(self, identity: UserContext | None = None):
        if identity is not None:
            self.identities.append(identity)
        with self.session_factory.begin() as session:
            yield session


@pytest.fixture
def repository_database():
    database = RepositoryDatabase()
    yield database
    database.engine.dispose()


def _create_scope(database: RepositoryDatabase) -> WorkspaceContext:
    with database.session_factory.begin() as session:
        user = UserRecord(
            phone_canonical=f"+86{str(uuid.uuid4().int)[-11:]}",
            password_hash="hash",
        )
        session.add(user)
        session.flush()
        workspace = WorkspaceRecord(user_id=user.id, name="默认工作区")
        session.add(workspace)
        session.flush()
    return WorkspaceContext(
        identity=UserContext(user_id=str(user.id), session_id="1"),
        workspace_id=str(workspace.id),
    )


def _series(**updates) -> Series:
    values = {
        "id": str(uuid.uuid4()),
        "title": "第一季",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    values.update(updates)
    return Series(**values)


def _script(**updates) -> Script:
    values = {
        "id": str(uuid.uuid4()),
        "title": "第一集",
        "original_text": "故事正文",
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    values.update(updates)
    return Script(**values)


def test_project_and_series_payloads_round_trip_with_versions(repository_database) -> None:
    context = _create_scope(repository_database)
    series_repository = PostgresSeriesRepository(repository_database)
    project_repository = PostgresProjectRepository(repository_database)
    series = _series()
    stored_series = series_repository.add(context, series)
    project = _script(series_id=stored_series.document.id)
    stored_project = project_repository.add(context, project)

    assert stored_series.document.title == series.title
    assert stored_series.version == 1
    assert stored_project.document.title == project.title
    assert stored_project.document.series_id == stored_series.document.id
    assert stored_project.schema_version == 1
    assert project_repository.get(context, stored_project.document.id) == stored_project
    assert [item.document.id for item in project_repository.list(context)] == [
        stored_project.document.id
    ]
    assert repository_database.identities[-1] == context.identity


def test_repository_queries_do_not_disclose_foreign_scope(repository_database) -> None:
    owner_context = _create_scope(repository_database)
    foreign_user_context = _create_scope(repository_database)
    with repository_database.session_factory.begin() as session:
        workspace = WorkspaceRecord(
            user_id=int(owner_context.identity.user_id),
            name="其他工作区",
        )
        session.add(workspace)
        session.flush()
        same_user_other_workspace_id = workspace.id
    other_workspace_context = replace(
        owner_context,
        workspace_id=str(same_user_other_workspace_id),
    )
    repository = PostgresProjectRepository(repository_database)
    project = _script()
    stored = repository.add(owner_context, project)
    project = stored.document

    assert repository.get(foreign_user_context, project.id) is None
    assert repository.get(other_workspace_context, project.id) is None
    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        repository.require(foreign_user_context, project.id)
    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        repository.update(
            other_workspace_context,
            project.id,
            project.model_copy(update={"title": "越权修改"}),
            1,
        )


def test_update_uses_optimistic_version_and_preserves_accepted_write(repository_database) -> None:
    context = _create_scope(repository_database)
    repository = PostgresProjectRepository(repository_database)
    project = _script()
    stored = repository.add(context, project)
    project = stored.document

    updated = repository.update(
        context,
        project.id,
        project.model_copy(update={"title": "已更新", "updated_at": 2.0}),
        expected_version=1,
    )

    assert updated.version == 2
    assert updated.document.title == "已更新"
    with pytest.raises(OptimisticVersionConflictError, match="刷新后重试"):
        repository.update(
            context,
            project.id,
            project.model_copy(update={"title": "过期写入", "updated_at": 3.0}),
            expected_version=1,
        )
    assert repository.require(context, project.id).document.title == "已更新"


def test_project_series_relationship_must_share_scope(repository_database) -> None:
    project_context = _create_scope(repository_database)
    foreign_context = _create_scope(repository_database)
    series = _series()
    foreign_series = PostgresSeriesRepository(repository_database).add(
        foreign_context,
        series,
    )

    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        PostgresProjectRepository(repository_database).add(
            project_context,
            _script(series_id=foreign_series.document.id),
        )


def test_repository_rejects_missing_context_before_query(repository_database) -> None:
    repository = PostgresProjectRepository(repository_database)

    with pytest.raises(InvalidRepositoryContextError, match="用户和工作区"):
        repository.list(None)  # type: ignore[arg-type]

    assert repository_database.identities == []


def test_repository_validates_json_payloads_on_read(repository_database) -> None:
    context = _create_scope(repository_database)
    with repository_database.session_factory.begin() as session:
        record = ProjectRecord(
            user_id=int(context.identity.user_id),
            workspace_id=int(context.workspace_id),
            title="损坏项目",
            payload={"id": "pending", "title": "损坏项目"},
            schema_version=1,
            version=1,
        )
        session.add(record)
        session.flush()
        record_id = record.id
        record.payload = {"id": str(record_id), "title": "损坏项目"}

    with pytest.raises(
        DocumentPayloadValidationError,
        match="数据库中的内容数据格式无效",
    ):
        PostgresProjectRepository(repository_database).get(context, str(record_id))


def test_soft_deleted_project_is_hidden_for_retention_window(repository_database) -> None:
    context = _create_scope(repository_database)
    repository = PostgresProjectRepository(repository_database)
    project = _script()
    stored = repository.add(context, project)
    project = stored.document

    repository.soft_delete(context, project.id)

    assert repository.get(context, project.id) is None
    assert repository.list(context) == []
    with repository_database.session_factory() as session:
        record = session.get(ProjectRecord, int(project.id))
        assert record is not None
        assert record.deleted_at is not None
        assert record.retention_expires_at is not None
        assert (record.retention_expires_at - record.deleted_at).days == 30
        assert record.version == 2
