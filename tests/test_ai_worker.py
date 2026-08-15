from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

from sqlalchemy import select

from src.platform.ai_task_api import AITaskCancellationService
from src.platform.ai_task_state import AITaskStateService
from src.platform.ai_worker import (
    AIWorkerService,
    AIWorkerTaskRepository,
    ProviderInvocationOutcome,
)
from src.platform.db_models import AITaskAttemptRecord, AITaskRecord, TicketHoldRecord
from src.platform.ticket_reservation import TicketReservationService
from src.platform.ticket_settlement import TicketSettlementService
from src.platform.ticket_wallet import TicketWalletService
from tests.test_ai_gateway import RecordingDispatcher, _gateway, _gateway_database, _payload
from tests.test_content_repositories import _create_scope
from tests.test_ticket_concurrency import SerializedSqliteDatabase


class RecordingClientFactory:
    def __init__(self) -> None:
        self.snapshots = []
        self.lock = Lock()

    def create(self, snapshot):
        with self.lock:
            self.snapshots.append(snapshot)
        return object()


class RecordingInvoker:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        record_submission: bool = True,
    ) -> None:
        self.error = error
        self.record_submission = record_submission
        self.task_ids: list[str] = []
        self.lock = Lock()

    def invoke(self, client, task, on_provider_submission):
        with self.lock:
            self.task_ids.append(task.task_id)
        if self.record_submission:
            on_provider_submission(
                "dashscope",
                "provider-task-100",
                "provider-request-100",
            )
        if self.error is not None:
            raise self.error
        return ProviderInvocationOutcome(
            raw_usage={"image_count": 1},
            result={"provider_stage": "completed"},
        )


class BlockingInvoker(RecordingInvoker):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def invoke(self, client, task, on_provider_submission):
        with self.lock:
            self.task_ids.append(task.task_id)
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试未恢复供应商调用")
        on_provider_submission(
            "dashscope",
            "provider-task-cancel-race",
            "provider-request-cancel-race",
        )
        return ProviderInvocationOutcome(
            raw_usage={"image_count": 1},
            result={"provider_stage": "completed"},
        )


def _queued_task(database, context, media_id, asset_id):
    dispatcher = RecordingDispatcher()
    submitted = _gateway(database, dispatcher).submit(
        context,
        _payload(media_id, asset_id),
    )
    return submitted


def _worker(database, factory, invoker, **services):
    return AIWorkerService(
        tasks=AIWorkerTaskRepository(database),
        task_state=AITaskStateService(database),
        settlement=TicketSettlementService(database),
        model_clients=factory,
        provider_invoker=invoker,
        **services,
    )


def _serialized_queued_task(path):
    database = SerializedSqliteDatabase(path)
    AITaskAttemptRecord.__table__.create(database.engine)
    context = _create_scope(database)
    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            3_000_000,
            reason="创建 worker 并发测试钱包",
        )
    config_snapshot = {
        "config_version_id": str(uuid.uuid4()),
        "route_id": str(uuid.uuid4()),
        "capability": "image.t2i",
        "provider": "dashscope",
        "provider_model_id": "wan-image-v1",
        "display_name": "平台图像模型",
        "parameters": {"count": 1, "resolution": "1024x1024"},
        "metering_formula": {
            "kind": "image",
            "base_tokens": 10,
            "per_image_tokens": 100,
            "max_images": 4,
            "resolution_multipliers": {"1024x1024": 1},
            "option_multipliers": {},
        },
        "fallback_policy": {"enabled": False, "max_attempts": 1},
        "secret_ref": "DASHSCOPE_API_KEY",
    }
    reservation = TicketReservationService(database).reserve_task(
        context,
        capability="image.t2i",
        idempotency_key="worker-concurrent-delivery",
        request_payload={"content": "并发领取", "parameters": {"count": 1}},
        config_snapshot=config_snapshot,
        maximum_metering_tokens=110,
        tokens_per_ticket=1000,
    )
    state = AITaskStateService(database)
    state.create_initial_attempt(
        context,
        task_id=reservation.task_id,
        config_snapshot=config_snapshot,
        provider="dashscope",
        provider_model_id="wan-image-v1",
    )
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"reserved"},
        target_status="queued",
    )
    return database, reservation.task_id


def test_worker_acquires_once_and_builds_client_from_task_snapshot() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _queued_task(database, context, media_id, asset_id)
    factory = RecordingClientFactory()
    invoker = RecordingInvoker()
    worker = _worker(database, factory, invoker)
    try:
        first = worker.execute(submitted.task_id)
        duplicate = worker.execute(submitted.task_id)

        assert first.status == "provider_succeeded"
        assert first.acquired is True
        assert duplicate.status == "provider_succeeded"
        assert duplicate.acquired is False
        assert invoker.task_ids == [submitted.task_id]
        assert len(factory.snapshots) == 1
        assert factory.snapshots[0].provider_model_id == "wan-image-v1"
        assert factory.snapshots[0].secret_ref == "DASHSCOPE_API_KEY"
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(submitted.task_id))
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            hold = session.scalar(
                select(TicketHoldRecord).where(TicketHoldRecord.task_id == task.id)
            )
            assert task is not None
            assert attempt is not None
            assert hold is not None
            assert task.status == "provider_succeeded"
            assert attempt.status == "polling"
            assert attempt.raw_usage == {
                "image_count": 1,
                "output_count": 1,
                "resolution": "1024x1024",
            }
            assert attempt.provider_task_id == "provider-task-100"
            assert attempt.provider_request_id == "provider-request-100"
            assert attempt.billable_acknowledged_at is not None
            assert task.provider_billable is True
            assert hold.attempt_id == attempt.id
        settled = TicketSettlementService(database).settle_success(
            context,
            task_id=submitted.task_id,
            attempt_id=submitted.attempt_id,
            raw_provider_usage={"image_count": 1},
        )
        assert settled.task_id == submitted.task_id
    finally:
        database.engine.dispose()


def test_duplicate_worker_deliveries_invoke_provider_once(tmp_path) -> None:
    database, task_id = _serialized_queued_task(tmp_path / "worker-delivery.db")
    factory = RecordingClientFactory()
    invoker = RecordingInvoker()
    worker = _worker(database, factory, invoker)
    barrier = Barrier(2)

    def execute():
        barrier.wait()
        return worker.execute(task_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: execute(), range(2)))

        assert sum(result.acquired for result in results) == 1
        assert invoker.task_ids == [task_id]
        assert len(factory.snapshots) == 1
    finally:
        database.engine.dispose()


def test_running_cancellation_does_not_refund_or_resubmit_provider_work() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _queued_task(database, context, media_id, asset_id)
    invoker = BlockingInvoker()
    worker = _worker(database, RecordingClientFactory(), invoker)
    cancellation = AITaskCancellationService(AITaskStateService(database))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker.execute, submitted.task_id)
            assert invoker.entered.wait(timeout=5)

            cancelled = cancellation.cancel(context, submitted.task_id)

            assert cancelled.outcome == "requested"
            assert cancelled.task.status == "running"
            assert cancelled.released_microtickets == 0
            with database.session_factory() as session:
                task = session.get(AITaskRecord, int(submitted.task_id))
                hold = session.scalar(
                    select(TicketHoldRecord).where(
                        TicketHoldRecord.task_id == int(submitted.task_id)
                    )
                )
                assert task is not None
                assert task.cancellation_requested_at is not None
                assert task.provider_billable is False
                assert hold is not None
                assert hold.status == "held"
                assert hold.remaining_microtickets == hold.quoted_microtickets

            invoker.release.set()
            result = future.result(timeout=5)

        assert result.status == "provider_succeeded"
        duplicate = worker.execute(submitted.task_id)
        assert duplicate.acquired is False
        assert invoker.task_ids == [submitted.task_id]
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(submitted.task_id))
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            hold = session.scalar(
                select(TicketHoldRecord).where(
                    TicketHoldRecord.task_id == int(submitted.task_id)
                )
            )
            assert task is not None
            assert task.provider_billable is True
            assert task.status == "provider_succeeded"
            assert attempt is not None
            assert attempt.provider_task_id == "provider-task-cancel-race"
            assert attempt.provider_request_id == "provider-request-cancel-race"
            assert attempt.billable_acknowledged_at is not None
            assert hold is not None
            assert hold.status == "held"
            assert hold.remaining_microtickets == hold.quoted_microtickets
    finally:
        invoker.release.set()
        database.engine.dispose()


def test_worker_redacts_provider_error_and_releases_nonbillable_hold() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _queued_task(database, context, media_id, asset_id)
    secret = "sk-this-must-never-be-persisted"
    worker = _worker(
        database,
        RecordingClientFactory(),
        RecordingInvoker(
            error=RuntimeError(f"Authorization: Bearer {secret}"),
            record_submission=False,
        ),
    )
    try:
        result = worker.execute(submitted.task_id)

        assert result.status == "failed"
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(submitted.task_id))
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            hold = session.scalar(
                select(TicketHoldRecord).where(TicketHoldRecord.task_id == task.id)
            )
            assert task is not None
            assert attempt is not None
            assert hold is not None
            assert task.safe_error_code == "PROVIDER_INVOCATION_FAILED"
            assert task.safe_error_message == "AI 生成失败，请稍后重试"
            assert secret not in str(task.safe_error_message)
            assert secret not in str(attempt.diagnostic)
            assert attempt.diagnostic == {
                "billing_state": "not_billable",
                "error_type": "RuntimeError",
                "stage": "provider_invocation",
            }
            assert attempt.status == "failed"
            assert hold.status == "released"
            assert hold.remaining_microtickets == 0
    finally:
        database.engine.dispose()


def test_worker_keeps_hold_when_provider_acknowledged_before_crash() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _queued_task(database, context, media_id, asset_id)
    secret = "sk-provider-crash-secret"
    worker = _worker(
        database,
        RecordingClientFactory(),
        RecordingInvoker(error=RuntimeError(f"Bearer {secret}")),
    )
    try:
        result = worker.execute(submitted.task_id)

        assert result.status == "ambiguous"
        duplicate = worker.execute(submitted.task_id)
        assert duplicate.acquired is False
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
            assert task.provider_billable is True
            assert attempt.status == "ambiguous"
            assert attempt.provider_task_id == "provider-task-100"
            assert attempt.diagnostic == {
                "billing_state": "acknowledged",
                "error_type": "RuntimeError",
                "stage": "provider_invocation",
            }
            assert secret not in str(attempt.diagnostic)
            assert hold.status == "held"
            assert hold.remaining_microtickets == hold.quoted_microtickets
    finally:
        database.engine.dispose()
