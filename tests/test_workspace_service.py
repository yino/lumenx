from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from src.platform.content_service import CloudContentService
from src.platform.contracts import AdminContext, MediaWrite, UserContext, WorkspaceContext
from src.platform.db_models import (
    AITaskRecord,
    ImportBatchRecord,
    MediaObjectRecord,
    ProjectRecord,
    WorkspaceRecord,
)
from src.platform.media_storage import CloudMediaStorage
from src.platform.workspaces import WorkspaceConflictError, WorkspaceService
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor
from tests.test_media_storage import FakePrivateObjectStore


class WorkspaceDatabase:
    def __init__(self, session: Mock) -> None:
        self.session = session
        self.identity = None

    @contextmanager
    def transaction(self, identity=None):
        self.identity = identity
        yield self.session


def user_context() -> UserContext:
    return UserContext(user_id="1", session_id="11")


def test_create_derives_owner_from_authenticated_context() -> None:
    session = Mock()
    database = WorkspaceDatabase(session)
    service = WorkspaceService(database)
    identity = user_context()

    record = service.create(identity, "  我的工作区  ")

    assert record.user_id == int(identity.user_id)
    assert record.name == "我的工作区"
    session.add.assert_called_once_with(record)
    session.flush.assert_called_once_with()


def test_soft_delete_keeps_thirty_day_restore_window() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    identity = user_context()
    target = WorkspaceRecord(
        id=2,
        user_id=int(identity.user_id),
        name="待删除",
        version=1,
    )
    session = Mock()
    session.scalar.side_effect = [target, 3]
    service = WorkspaceService(WorkspaceDatabase(session))

    service.soft_delete(identity, target.id, now=now)

    assert target.deleted_at == now
    assert target.retention_expires_at == now + timedelta(days=30)
    assert target.version == 2


def test_last_active_workspace_cannot_be_deleted() -> None:
    identity = user_context()
    target = WorkspaceRecord(
        id=2,
        user_id=int(identity.user_id),
        name="唯一工作区",
        version=1,
    )
    session = Mock()
    session.scalar.side_effect = [target, None]
    service = WorkspaceService(WorkspaceDatabase(session))

    with pytest.raises(WorkspaceConflictError, match="至少需要保留"):
        service.soft_delete(identity, target.id)


def test_expired_workspace_cleanup_deletes_content_but_preserves_task_history() -> None:
    database = RepositoryDatabase()
    AITaskRecord.__table__.create(database.engine)
    ImportBatchRecord.__table__.create(database.engine)
    base_context = _create_scope(database)
    service = WorkspaceService(database)
    expired_at = datetime(2026, 7, 1, tzinfo=UTC)
    checked_at = expired_at + timedelta(days=31)

    disposable = service.create(base_context.identity, "可清理工作区")
    disposable_context = WorkspaceContext(
        identity=base_context.identity,
        workspace_id=str(disposable.id),
    )
    project = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    ).create_project(
        disposable_context,
        title="待清理项目",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
    )
    media = CloudMediaStorage(database, FakePrivateObjectStore()).store(
        disposable_context,
        MediaWrite(
            content=b"content",
            content_type="image/png",
            filename="image.png",
            project_id=project.document.id,
        ),
    )
    service.soft_delete(base_context.identity, disposable.id, now=expired_at)

    preserved = service.create(base_context.identity, "保留账务历史")
    service.soft_delete(base_context.identity, preserved.id, now=expired_at)
    with database.session_factory.begin() as session:
        session.add(
            AITaskRecord(
                user_id=int(base_context.identity.user_id),
                workspace_id=preserved.id,
                capability="playground.image",
                status="succeeded",
                idempotency_key=str(uuid.uuid4()),
                request_fingerprint="a" * 64,
                request_payload={},
                config_snapshot={},
                tokens_per_ticket=1000,
                quoted_microtickets=0,
            )
        )

    admin = AdminContext(admin_id="9001", session_id="9101", username="admin")
    result = service.cleanup_expired(admin, now=checked_at)

    assert result.deleted == 1
    assert result.skipped == 1
    with database.session_factory() as session:
        assert session.get(WorkspaceRecord, disposable.id) is None
        assert session.get(ProjectRecord, int(project.document.id)) is None
        assert session.get(MediaObjectRecord, int(media.media_id)) is None
        assert session.get(WorkspaceRecord, preserved.id) is not None
        assert session.scalar(
            select(AITaskRecord.id).where(AITaskRecord.workspace_id == preserved.id)
        ) is not None

    database.engine.dispose()
