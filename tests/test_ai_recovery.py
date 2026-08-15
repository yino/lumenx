from __future__ import annotations

import uuid

from sqlalchemy import func, select

from src.platform.ai_recovery import (
    AIRecoveryRepository,
    AIWorkerRecoveryService,
    ProviderRecoveryOutcome,
)
from src.platform.ai_task_state import AITaskStateService
from src.platform.ai_worker import AIWorkerTaskRepository
from src.platform.db_models import AITaskAttemptRecord, AITaskRecord, TicketHoldRecord
from tests.test_ai_gateway import RecordingDispatcher, _gateway, _gateway_database, _payload
from tests.test_ai_worker import (
    RecordingClientFactory,
    RecordingInvoker,
    _worker,
)


class RecordingRecoveryInvoker:
    def __init__(self, status: str = "succeeded") -> None:
        self.status = status
        self.calls = []

    def resume(self, client, task):
        self.calls.append(
            {
                "task_id": task.task.task_id,
                "provider_task_id": task.provider_task_id,
                "provider_request_id": task.provider_request_id,
            }
        )
        return ProviderRecoveryOutcome(
            status=self.status,
            raw_usage={"image_count": 1},
            result={"provider_stage": "recovered"},
        )


def _queued_task(database, context, media_id, asset_id):
    return _gateway(database, RecordingDispatcher()).submit(
        context,
        _payload(media_id, asset_id),
    )


def _recovery_service(database, invoker, *, stale_after_seconds=300):
    return AIWorkerRecoveryService(
        recovery=AIRecoveryRepository(
            database,
            stale_after_seconds=stale_after_seconds,
        ),
        task_state=AITaskStateService(database),
        model_clients=RecordingClientFactory(),
        provider_recovery=invoker,
    )


def test_recovery_resumes_known_provider_task_without_resubmission() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _queued_task(database, context, media_id, asset_id)
    crashed_worker = _worker(
        database,
        RecordingClientFactory(),
        RecordingInvoker(error=RuntimeError("worker crashed after acceptance")),
    )
    recovery_invoker = RecordingRecoveryInvoker()
    recovery = _recovery_service(database, recovery_invoker)
    try:
        assert crashed_worker.execute(submitted.task_id).status == "ambiguous"
        assert submitted.task_id in AIRecoveryRepository(database).list_candidate_ids()

        result = recovery.recover(submitted.task_id)
        duplicate = recovery.recover(submitted.task_id)

        assert result.status == "provider_succeeded"
        assert result.recovered is True
        assert duplicate.status == "provider_succeeded"
        assert duplicate.recovered is False
        assert recovery_invoker.calls == [
            {
                "task_id": submitted.task_id,
                "provider_task_id": "provider-task-100",
                "provider_request_id": "provider-request-100",
            }
        ]
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(submitted.task_id))
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            assert task is not None
            assert attempt is not None
            assert task.status == "provider_succeeded"
            assert attempt.status == "polling"
            assert attempt.raw_usage == {
                "image_count": 1,
                "output_count": 1,
                "resolution": "1024x1024",
            }
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
    finally:
        database.engine.dispose()


def test_recovery_marks_missing_provider_identifier_ambiguous() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _queued_task(database, context, media_id, asset_id)
    invoker = RecordingRecoveryInvoker()
    try:
        acquisition = AIWorkerTaskRepository(database).acquire(submitted.task_id)
        assert acquisition.lease is not None

        result = _recovery_service(database, invoker).recover(submitted.task_id)

        assert result.status == "ambiguous"
        assert result.recovered is False
        assert invoker.calls == []
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(submitted.task_id))
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            hold = session.scalar(
                select(TicketHoldRecord).where(TicketHoldRecord.task_id == task.id)
            )
            assert task is not None
            assert attempt is not None
            assert hold is not None
            assert task.status == "running"
            assert attempt.status == "ambiguous"
            assert attempt.diagnostic["reason"] == "provider_task_id_missing"
            assert hold.status == "held"
    finally:
        database.engine.dispose()


def test_stale_polling_recovery_can_resume_but_fresh_duplicate_cannot() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _queued_task(database, context, media_id, asset_id)
    crashed_worker = _worker(
        database,
        RecordingClientFactory(),
        RecordingInvoker(error=RuntimeError("worker crashed after acceptance")),
    )
    invoker = RecordingRecoveryInvoker(status="pending")
    try:
        crashed_worker.execute(submitted.task_id)
        first = _recovery_service(database, invoker).recover(submitted.task_id)
        duplicate = _recovery_service(database, invoker).recover(submitted.task_id)
        stale = _recovery_service(
            database,
            invoker,
            stale_after_seconds=0,
        ).recover(submitted.task_id)

        assert first.recovered is True
        assert duplicate.recovered is False
        assert stale.recovered is True
        assert len(invoker.calls) == 2
        assert all(
            call["provider_task_id"] == "provider-task-100"
            for call in invoker.calls
        )
    finally:
        database.engine.dispose()
