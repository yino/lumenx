from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from src.platform.auth.sessions import issue_session
from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    AssetRecord,
    AuthSessionRecord,
    Base,
    MediaObjectRecord,
    ProjectRecord,
)
from src.platform.maintenance import (
    BACKUP_MAINTENANCE_TASK,
    MAINTENANCE_QUEUE,
    MEDIA_MAINTENANCE_TASK,
    RECONCILIATION_MAINTENANCE_TASK,
    RETENTION_MAINTENANCE_TASK,
    SESSION_MAINTENANCE_TASK,
    BackupVerificationError,
    BackupVerificationService,
    MAINTENANCE_IDENTITY,
    OrphanMediaCleanupService,
    RetentionCleanupService,
    SessionExpiryService,
    TaskHoldMaintenanceService,
)
from src.platform.ticket_settlement import TicketSettlementService
from src.platform.worker import celery_app
from tests.test_ai_gateway import RecordingDispatcher, _gateway, _gateway_database, _payload
from tests.test_ai_worker import RecordingClientFactory, RecordingInvoker, _worker
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor
from src.platform.content_service import CloudContentService
from src.platform.contracts import MediaWrite
from src.platform.media_storage import CloudMediaStorage
from tests.test_media_storage import FakePrivateObjectStore


def _full_database():
    database = RepositoryDatabase()
    Base.metadata.create_all(database.engine, checkfirst=True)
    return database


def test_session_expiry_revokes_only_expired_sessions_idempotently() -> None:
    database = _full_database()
    context = _create_scope(database)
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    expired = issue_session(
        int(context.identity.user_id),
        "s" * 32,
        now=now - timedelta(days=40),
    ).record
    active = issue_session(
        int(context.identity.user_id),
        "s" * 32,
        now=now - timedelta(minutes=5),
    ).record
    with database.session_factory.begin() as session:
        session.add_all([expired, active])

    service = SessionExpiryService(database)
    first = service.run(now=now)
    second = service.run(now=now)

    assert first.revoked_sessions == 1
    assert second.revoked_sessions == 0
    with database.session_factory() as session:
        revoked_at = session.get(AuthSessionRecord, expired.id).revoked_at
        assert revoked_at.replace(tzinfo=UTC) == now
        assert session.get(AuthSessionRecord, active.id).revoked_at is None
    database.engine.dispose()


def test_retention_cleanup_deletes_expired_documents_conservatively() -> None:
    database = _full_database()
    context = _create_scope(database)
    content = CloudContentService(database, script_processor=FakeScriptProcessor())
    project = content.create_project(
        context,
        title="待清理项目",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
    )
    expired_at = datetime(2026, 7, 1, tzinfo=UTC)
    with database.session_factory.begin() as session:
        record = session.get(ProjectRecord, int(project.document.id))
        record.deleted_at = expired_at
        record.retention_expires_at = expired_at + timedelta(days=30)

    store = FakePrivateObjectStore()
    service = RetentionCleanupService(database, store)
    first = service.run(now=datetime(2026, 8, 14, tzinfo=UTC))
    second = service.run(now=datetime(2026, 8, 14, tzinfo=UTC))

    assert first.deleted_projects == 1
    assert second.deleted_projects == 0
    database.engine.dispose()


def test_orphan_media_cleanup_is_retryable_after_object_store_failure() -> None:
    database = _full_database()
    context = _create_scope(database)
    storage = CloudMediaStorage(database, FakePrivateObjectStore())
    stored = storage.store(
        context,
        MediaWrite(
            content=b"media",
            content_type="image/png",
            filename="frame.png",
            project_id=None,
            provenance={"origin": "test"},
        ),
    )
    checked_at = datetime(2026, 8, 14, tzinfo=UTC)
    with database.session_factory.begin() as session:
        record = session.get(MediaObjectRecord, int(stored.media_id))
        record.lifecycle_state = "failed"
        record.created_at = checked_at - timedelta(days=2)

    class FailingOnceStore(FakePrivateObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def delete(self, object_key: str) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("temporary unavailable")
            super().delete(object_key)

    store = FailingOnceStore()
    service = OrphanMediaCleanupService(database, store)
    first = service.run(now=checked_at)
    second = service.run(now=checked_at)

    assert (first.deleted, first.failed) == (0, 1)
    assert (second.deleted, second.failed) == (1, 0)
    with database.session_factory() as session:
        assert session.get(MediaObjectRecord, int(stored.media_id)) is None
    database.engine.dispose()


def test_stale_task_maintenance_dispatches_recovery_and_reconciles_read_only() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _gateway(database, RecordingDispatcher()).submit(
        context,
        _payload(media_id, asset_id),
    )
    crashed_worker = _worker(
        database,
        RecordingClientFactory(),
        RecordingInvoker(error=RuntimeError("worker crashed after acceptance")),
    )
    assert crashed_worker.execute(submitted.task_id).status == "ambiguous"
    dispatcher = RecordingDispatcher()

    report = TaskHoldMaintenanceService(
        database,
        dispatcher,
        recovery_stale_after_seconds=0,
    ).run(now=datetime.now(UTC), stale_hold_after=timedelta(minutes=1))

    assert report.recovery_candidates == 1
    assert report.recovery_dispatched == 1
    assert dispatcher.task_ids == [submitted.task_id]
    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(submitted.task_id))
        attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
        assert task.status == "running"
        assert attempt.status == "ambiguous"
    database.engine.dispose()


def test_backup_verification_checks_checksum_age_and_required_tables(tmp_path: Path) -> None:
    backup = tmp_path / "lumenx-20260814T000000Z.dump"
    backup.write_bytes(b"postgres-backup")
    checksum = hashlib.sha256(backup.read_bytes()).hexdigest()
    backup.with_suffix(".dump.sha256").write_text(f"{checksum}  {backup.name}\n", encoding="utf-8")
    backup.with_suffix(".dump.list").write_text(
        "\n".join(BackupVerificationService.REQUIRED_TABLES),
        encoding="utf-8",
    )
    now = datetime.now(UTC)
    os.utime(backup, (now.timestamp(), now.timestamp()))

    report = BackupVerificationService(tmp_path).run(now=now)
    assert report.sha256 == checksum
    assert report.size_bytes == len(b"postgres-backup")

    backup.with_suffix(".dump.sha256").write_text("0" * 64, encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="校验和"):
        BackupVerificationService(tmp_path).run(now=now)


def test_celery_schedules_all_maintenance_tasks_on_isolated_queue() -> None:
    tasks = {
        SESSION_MAINTENANCE_TASK,
        RETENTION_MAINTENANCE_TASK,
        MEDIA_MAINTENANCE_TASK,
        RECONCILIATION_MAINTENANCE_TASK,
        BACKUP_MAINTENANCE_TASK,
    }
    assert all(celery_app.conf.task_routes[task] == {"queue": MAINTENANCE_QUEUE} for task in tasks)
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert scheduled == tasks
