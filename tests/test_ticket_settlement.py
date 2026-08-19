from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from src.platform.ai_task_state import AITaskStateService
from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
)
from src.platform.ticket_reservation import TicketReservationService
from src.platform.ticket_settlement import (
    TicketSettlementConflictError,
    TicketSettlementScopeNotFoundError,
    TicketSettlementService,
)
from tests.test_ticket_reservation import _reservation_database


LLM_FORMULA = {
    "kind": "llm",
    "input_weight": 1,
    "output_weight": 2,
    "max_input_tokens": 1000,
    "max_output_tokens": 500,
}


def _settlement_database():
    database, context = _reservation_database(initial_balance=3_000_000)
    AITaskAttemptRecord.__table__.create(database.engine)
    UsageEventRecord.__table__.create(database.engine)
    reservation = TicketReservationService(database).reserve_task(
        context,
        capability="script.analysis",
        idempotency_key="settlement-task-1",
        request_payload={"content": "测试剧本", "parameters": {}},
        config_snapshot={
            "config_version_id": str(uuid.uuid4()),
            "capability": "script.analysis",
            "parameters": {},
            "metering_formula": LLM_FORMULA,
        },
        maximum_metering_tokens=2000,
        tokens_per_ticket=1000,
    )
    with database.session_factory.begin() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        assert task is not None
        task.status = "provider_succeeded"
    return database, context, reservation


def test_successful_settlement_records_usage_charge_and_unused_release() -> None:
    database, context, reservation = _settlement_database()
    service = TicketSettlementService(database)

    result = service.settle_success(
        context,
        task_id=reservation.task_id,
        raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
    )

    assert result.reused is False
    assert result.metering_tokens == 200
    assert result.charged_microtickets == 200_000
    assert result.released_microtickets == 1_800_000
    assert result.tokens_per_ticket == 1000
    assert result.wallet.available_microtickets == 2_800_000
    assert result.wallet.held_microtickets == 0
    assert result.wallet.lifetime_spent_microtickets == 200_000
    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        usage = session.get(UsageEventRecord, int(result.usage_event_id))
        assert task is not None
        assert hold is not None
        assert usage is not None
        assert task.status == "succeeded"
        assert task.provider_billable is True
        assert hold.status == "settled"
        assert hold.remaining_microtickets == 0
        assert usage.raw_provider_usage == {"input_tokens": 100, "output_tokens": 50}
        assert usage.metering_formula == LLM_FORMULA
        entries = list(session.scalars(select(TicketLedgerRecord)))
        assert [entry.entry_type for entry in entries].count("settlement") == 1
        assert [entry.entry_type for entry in entries].count("release") == 1
        for entry in entries:
            if entry.entry_type in {"settlement", "release"}:
                assert entry.task_id == task.id
                assert entry.hold_id == hold.id
                assert entry.correlation["usage_event_id"] == str(usage.id)
    database.engine.dispose()


def test_successful_settlement_is_idempotent_after_completion() -> None:
    database, context, reservation = _settlement_database()
    service = TicketSettlementService(database)
    first = service.settle_success(
        context,
        task_id=reservation.task_id,
        raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
    )

    second = service.settle_success(
        context,
        task_id=reservation.task_id,
        raw_provider_usage={"not_json": object()},
    )

    assert second.reused is True
    assert second.usage_event_id == first.usage_event_id
    assert second.charged_microtickets == first.charged_microtickets
    assert second.wallet.version == first.wallet.version
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 1
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 4
    database.engine.dispose()


def test_actual_usage_above_snapshot_bound_does_not_mutate_billing() -> None:
    database, context, reservation = _settlement_database()
    service = TicketSettlementService(database)

    with pytest.raises(TicketSettlementConflictError, match="超出任务配置快照"):
        service.settle_success(
            context,
            task_id=reservation.task_id,
            raw_provider_usage={"input_tokens": 1001, "output_tokens": 0},
        )

    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        wallet = session.get(TicketWalletRecord, int(context.identity.user_id))
        assert task is not None
        assert hold is not None
        assert wallet is not None
        assert task.status == "provider_succeeded"
        assert hold.status == "held"
        assert hold.remaining_microtickets == 2_000_000
        assert wallet.available_microtickets == 1_000_000
        assert wallet.held_microtickets == 2_000_000
        assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 2
    database.engine.dispose()


def test_foreign_user_cannot_settle_task() -> None:
    database, _context, reservation = _settlement_database()
    from tests.test_content_repositories import _create_scope

    foreign_context = _create_scope(database)
    with pytest.raises(TicketSettlementScopeNotFoundError, match="AI 任务不存在"):
        TicketSettlementService(database).settle_success(
            foreign_context,
            task_id=reservation.task_id,
            raw_provider_usage={"input_tokens": 1, "output_tokens": 1},
        )
    database.engine.dispose()


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("nonbillable_failure", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_nonbillable_terminal_outcome_releases_full_hold(
    outcome: str,
    expected_status: str,
) -> None:
    database, context, reservation = _settlement_database()
    with database.session_factory.begin() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        assert task is not None
        task.status = "running"
        task.provider_billable = False

    result = TicketSettlementService(database).release_nonbillable(
        context,
        task_id=reservation.task_id,
        outcome=outcome,  # type: ignore[arg-type]
        reason="供应商确认未产生计费",
        attempt_diagnostic=(
            {"billing_state": "not_billable"} if outcome == "cancelled" else None
        ),
    )

    assert result.outcome == outcome
    assert result.charged_microtickets == 0
    assert result.released_microtickets == 2_000_000
    assert result.wallet.available_microtickets == 3_000_000
    assert result.wallet.held_microtickets == 0
    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        usage = session.get(UsageEventRecord, int(result.usage_event_id))
        assert task is not None
        assert hold is not None
        assert usage is not None
        assert task.status == expected_status
        assert hold.status == "released"
        assert hold.remaining_microtickets == 0
        assert usage.outcome == outcome
        assert usage.metering_tokens == 0
        assert usage.charged_microtickets == 0
    database.engine.dispose()


def test_billable_postprocessing_failure_settles_and_marks_support_review() -> None:
    database, context, reservation = _settlement_database()

    result = TicketSettlementService(database).settle_billable_failure(
        context,
        task_id=reservation.task_id,
        raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
        support_review_reason="供应商成功后 OSS 上传失败",
        safe_error_code="OUTPUT_STORAGE_FAILED",
        safe_error_message="生成结果保存失败，已进入人工复核",
    )

    assert result.outcome == "billable_failure"
    assert result.support_review is True
    assert result.metering_tokens == 200
    assert result.charged_microtickets == 200_000
    assert result.released_microtickets == 1_800_000
    assert result.wallet.available_microtickets == 2_800_000
    assert result.wallet.held_microtickets == 0
    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        usage = session.get(UsageEventRecord, int(result.usage_event_id))
        assert task is not None
        assert hold is not None
        assert usage is not None
        assert task.status == "support_review"
        assert task.provider_billable is True
        assert task.support_review_reason == "供应商成功后 OSS 上传失败"
        assert task.safe_error_code == "OUTPUT_STORAGE_FAILED"
        assert hold.status == "settled"
        assert usage.outcome == "billable_failure"
    database.engine.dispose()


def test_unmetered_billable_failure_releases_full_hold_and_marks_review() -> None:
    database, context, reservation = _settlement_database()
    created_attempt = AITaskStateService(database).create_initial_attempt(
        context,
        task_id=reservation.task_id,
        config_snapshot={
            "config_version_id": "test-unmetered-failure",
            "capability": "script.analysis",
            "parameters": {},
            "metering_formula": LLM_FORMULA,
        },
        provider="dashscope",
        provider_model_id="qwen-test",
    )
    with database.session_factory.begin() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        attempt = session.get(AITaskAttemptRecord, int(created_attempt.id))
        assert task is not None and attempt is not None
        task.status = "running"
        task.provider_billable = True
        attempt.status = "ambiguous"

    result = TicketSettlementService(database).release_unmetered_billable_failure(
        context,
        task_id=reservation.task_id,
        attempt_id=str(attempt.id),
        support_review_reason="供应商用量超出报价快照",
        safe_error_code="PROVIDER_USAGE_REVIEW_REQUIRED",
        safe_error_message="预扣已退回",
        attempt_diagnostic={"stage": "provider_invocation"},
    )

    assert result.support_review is True
    assert result.charged_microtickets == 0
    assert result.released_microtickets == 2_000_000
    assert result.wallet.held_microtickets == 0
    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        usage = session.get(UsageEventRecord, int(result.usage_event_id))
        assert task is not None and hold is not None and usage is not None
        assert task.status == "support_review"
        assert task.provider_billable is True
        assert hold.status == "released"
        assert usage.outcome == "billable_failure"
        assert usage.raw_provider_usage == {"unmetered": True}
    database.engine.dispose()


def test_billable_acknowledgement_blocks_nonbillable_release() -> None:
    database, context, reservation = _settlement_database()
    with database.session_factory.begin() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        assert task is not None
        task.status = "running"
        task.provider_billable = True

    with pytest.raises(TicketSettlementConflictError, match="不能全额释放"):
        TicketSettlementService(database).release_nonbillable(
            context,
            task_id=reservation.task_id,
            outcome="nonbillable_failure",
            reason="错误释放",
        )

    with database.session_factory() as session:
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        wallet = session.get(TicketWalletRecord, int(context.identity.user_id))
        assert hold is not None
        assert wallet is not None
        assert hold.status == "held"
        assert hold.remaining_microtickets == 2_000_000
        assert wallet.available_microtickets == 1_000_000
        assert wallet.held_microtickets == 2_000_000
        assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 0
    database.engine.dispose()


def test_billable_failure_settlement_is_idempotent() -> None:
    database, context, reservation = _settlement_database()
    service = TicketSettlementService(database)
    first = service.settle_billable_failure(
        context,
        task_id=reservation.task_id,
        raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
        support_review_reason="供应商成功后本地处理失败",
    )

    second = service.settle_billable_failure(
        context,
        task_id=reservation.task_id,
        raw_provider_usage={"not_json": object()},
        support_review_reason=" ",
    )

    assert second.reused is True
    assert second.usage_event_id == first.usage_event_id
    assert second.charged_microtickets == first.charged_microtickets
    assert second.wallet.version == first.wallet.version
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 1
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 4
    database.engine.dispose()


def test_billable_failure_requires_review_reason_before_billing_mutation() -> None:
    database, context, reservation = _settlement_database()

    with pytest.raises(TicketSettlementConflictError, match="必须填写原因"):
        TicketSettlementService(database).settle_billable_failure(
            context,
            task_id=reservation.task_id,
            raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
            support_review_reason=" ",
        )

    with database.session_factory() as session:
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        wallet = session.get(TicketWalletRecord, int(context.identity.user_id))
        assert hold is not None
        assert wallet is not None
        assert hold.status == "held"
        assert wallet.available_microtickets == 1_000_000
        assert wallet.held_microtickets == 2_000_000
        assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 2
    database.engine.dispose()
