from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from src.platform.ai_task_state import (
    AITaskStateConflictError,
    AITaskStateScopeNotFoundError,
    AITaskStateService,
)
from src.platform.db_models import AITaskAttemptRecord, AITaskRecord, UsageEventRecord
from src.platform.ticket_reservation import TicketReservationService
from tests.test_content_repositories import _create_scope
from tests.test_ticket_concurrency import SerializedSqliteDatabase
from tests.test_ticket_reservation import _reservation_database
from tests.test_ticket_settlement import LLM_FORMULA


def _state_database():
    database, context = _reservation_database(initial_balance=3_000_000)
    AITaskAttemptRecord.__table__.create(database.engine)
    UsageEventRecord.__table__.create(database.engine)
    reservation = TicketReservationService(database).reserve_task(
        context,
        capability="script.analysis",
        idempotency_key="state-machine-task",
        request_payload={"content": "状态机测试", "parameters": {}},
        config_snapshot={
            "config_version_id": str(uuid.uuid4()),
            "capability": "script.analysis",
            "parameters": {},
            "metering_formula": LLM_FORMULA,
        },
        maximum_metering_tokens=2000,
        tokens_per_ticket=1000,
    )
    return database, context, reservation


def test_task_and_attempt_state_machine_persists_snapshots_and_correlations() -> None:
    database, context, reservation = _state_database()
    service = AITaskStateService(database)
    attempt_config = {
        "config_version_id": str(uuid.uuid4()),
        "provider": "dashscope",
        "provider_model_id": "qwen-plus",
        "metering_formula": LLM_FORMULA,
    }
    try:
        attempt = service.create_initial_attempt(
            context,
            task_id=reservation.task_id,
            config_snapshot=attempt_config,
            provider="dashscope",
            provider_model_id="qwen-plus",
        )
        queued = service.transition_task(
            context,
            task_id=reservation.task_id,
            expected_statuses={"reserved"},
            target_status="queued",
        )
        running_task = service.transition_task(
            context,
            task_id=reservation.task_id,
            expected_statuses={"queued"},
            target_status="running",
        )
        running_attempt = service.transition_attempt(
            context,
            task_id=reservation.task_id,
            attempt_id=attempt.id,
            expected_statuses={"pending"},
            target_status="running",
        )
        submitted = service.record_provider_submission(
            context,
            task_id=reservation.task_id,
            attempt_id=attempt.id,
            provider_request_id="req-100",
            provider_task_id="provider-task-100",
            billable_acknowledged=True,
        )
        aggregate = service.get(context, reservation.task_id)

        assert queued.status == "queued"
        assert running_task.status == "running"
        assert running_task.started_at is not None
        assert running_attempt.status == "running"
        assert submitted.provider_request_id == "req-100"
        assert submitted.provider_task_id == "provider-task-100"
        assert submitted.billable_acknowledged_at is not None
        assert aggregate.task.provider_billable is True
        assert aggregate.task.config_snapshot != attempt.config_snapshot
        assert aggregate.attempts[0].config_snapshot == attempt_config
        assert aggregate.billing.hold_ids == (reservation.hold_id,)
        assert aggregate.billing.open_hold_ids == (reservation.hold_id,)
        assert aggregate.billing.usage_event_ids == ()
    finally:
        database.engine.dispose()


def test_state_transitions_use_compare_and_set_and_reject_resurrection() -> None:
    database, context, reservation = _state_database()
    service = AITaskStateService(database)
    try:
        service.transition_task(
            context,
            task_id=reservation.task_id,
            expected_statuses={"reserved"},
            target_status="cancelled",
        )

        with pytest.raises(AITaskStateConflictError, match="不符合期望状态"):
            service.transition_task(
                context,
                task_id=reservation.task_id,
                expected_statuses={"reserved"},
                target_status="queued",
            )
        with pytest.raises(AITaskStateConflictError, match="不能从 cancelled"):
            service.transition_task(
                context,
                task_id=reservation.task_id,
                expected_statuses={"cancelled"},
                target_status="queued",
            )
    finally:
        database.engine.dispose()


def test_provider_submission_and_cancellation_requests_are_idempotent() -> None:
    database, context, reservation = _state_database()
    service = AITaskStateService(database)
    try:
        attempt = service.create_initial_attempt(
            context,
            task_id=reservation.task_id,
            config_snapshot={"version": 1},
            provider="dashscope",
            provider_model_id="qwen-plus",
        )
        service.transition_task(
            context,
            task_id=reservation.task_id,
            expected_statuses={"reserved"},
            target_status="queued",
        )
        service.transition_task(
            context,
            task_id=reservation.task_id,
            expected_statuses={"queued"},
            target_status="running",
        )
        service.transition_attempt(
            context,
            task_id=reservation.task_id,
            attempt_id=attempt.id,
            expected_statuses={"pending"},
            target_status="running",
        )
        first_submission = service.record_provider_submission(
            context,
            task_id=reservation.task_id,
            attempt_id=attempt.id,
            provider_request_id="same-request",
            billable_acknowledged=True,
        )
        second_submission = service.record_provider_submission(
            context,
            task_id=reservation.task_id,
            attempt_id=attempt.id,
            provider_request_id="same-request",
            billable_acknowledged=True,
        )
        first_cancel = service.request_cancellation(
            context,
            task_id=reservation.task_id,
        )
        second_cancel = service.request_cancellation(
            context,
            task_id=reservation.task_id,
        )

        assert first_submission.billable_acknowledged_at == second_submission.billable_acknowledged_at
        assert first_cancel.cancellation_requested_at == second_cancel.cancellation_requested_at
        assert second_cancel.status == "running"
        with pytest.raises(AITaskStateConflictError, match="冲突"):
            service.record_provider_submission(
                context,
                task_id=reservation.task_id,
                attempt_id=attempt.id,
                provider_request_id="different-request",
            )
    finally:
        database.engine.dispose()


def test_support_review_requires_reason_and_foreign_scope_is_hidden() -> None:
    database, context, reservation = _state_database()
    service = AITaskStateService(database)
    foreign_context = _create_scope(database)
    try:
        with database.transaction(context.identity) as session:
            task = session.get(AITaskRecord, int(reservation.task_id))
            assert task is not None
            task.status = "provider_succeeded"

        with pytest.raises(AITaskStateConflictError, match="人工复核原因"):
            service.transition_task(
                context,
                task_id=reservation.task_id,
                expected_statuses={"provider_succeeded"},
                target_status="support_review",
            )
        reviewed = service.transition_task(
            context,
            task_id=reservation.task_id,
            expected_statuses={"provider_succeeded"},
            target_status="support_review",
            support_review_reason="供应商已计费，输出保存失败",
            safe_error_code="OUTPUT_SAVE_FAILED",
            safe_error_message="结果保存失败，已进入人工复核",
        )

        assert reviewed.status == "support_review"
        assert reviewed.support_review_reason == "供应商已计费，输出保存失败"
        assert reviewed.completed_at is not None
        with pytest.raises(AITaskStateScopeNotFoundError):
            service.get(foreign_context, reservation.task_id)
    finally:
        database.engine.dispose()


def test_only_one_concurrent_worker_acquires_queued_task(tmp_path) -> None:
    database = SerializedSqliteDatabase(tmp_path / "task-cas.db")
    AITaskAttemptRecord.__table__.create(database.engine)
    context = _create_scope(database)
    from src.platform.ticket_wallet import TicketWalletService

    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            3_000_000,
            reason="创建任务状态测试钱包",
        )
    reservation = TicketReservationService(database).reserve_task(
        context,
        capability="script.analysis",
        idempotency_key="worker-acquire",
        request_payload={"content": "并发领取"},
        config_snapshot={"version": 1},
        maximum_metering_tokens=1000,
        tokens_per_ticket=1000,
    )
    service = AITaskStateService(database)
    service.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"reserved"},
        target_status="queued",
    )
    barrier = Barrier(2)

    def acquire():
        barrier.wait()
        try:
            result = service.transition_task(
                context,
                task_id=reservation.task_id,
                expected_statuses={"queued"},
                target_status="running",
            )
            return result.status
        except AITaskStateConflictError:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: acquire(), range(2)))

        assert sorted(outcomes) == ["conflict", "running"]
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(reservation.task_id))
            assert task is not None
            assert task.status == "running"
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
    finally:
        database.engine.dispose()
