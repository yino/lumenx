from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
)
from src.platform.ticket_reservation import InsufficientTicketBalanceError
from src.platform.ticket_retry import (
    RetryAccountingConflictError,
    RetryAccountingService,
)
from src.platform.ticket_settlement import TicketSettlementService
from src.platform.ticket_wallet import TicketWalletService
from tests.test_ticket_settlement import _settlement_database


def _retry_database(
    *,
    initial_balance: int = 5_000_000,
    attempt_status: str = "failed",
    billing_state: str | None = "not_billable",
    provider_task_id: str | None = None,
):
    database, context, reservation = _settlement_database()
    if initial_balance != 3_000_000:
        with database.session_factory.begin() as session:
            wallet = session.scalar(
                select(TicketWalletRecord).where(
                    TicketWalletRecord.user_id == int(context.identity.user_id)
                )
            )
            assert wallet is not None
            delta = initial_balance - 3_000_000
            if delta > 0:
                TicketWalletService.credit_in_session(
                    session,
                    wallet,
                    delta,
                    entry_type="grant",
                    reason="补充重试测试余额",
                )
            elif delta < 0:
                TicketWalletService.debit_adjustment_in_session(
                    session,
                    wallet,
                    -delta,
                    reason="调整重试测试余额",
                    actor_user_id=context.identity.user_id,
                )
    diagnostic = {"billing_state": billing_state} if billing_state else {}
    with database.session_factory.begin() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        assert task is not None
        assert hold is not None
        task.status = "running"
        task.provider_billable = False
        attempt = AITaskAttemptRecord(
                user_id=task.user_id,
                workspace_id=task.workspace_id,
                task_id=task.id,
                attempt_number=1,
                status=attempt_status,
                config_snapshot=task.config_snapshot,
                provider="dashscope",
                provider_model_id="qwen-test",
                provider_task_id=provider_task_id,
                diagnostic=diagnostic,
            )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
        hold.attempt_id = attempt.id
    return database, context, reservation, attempt_id


def _resubmit(service, context, reservation, attempt_id, **updates):
    values = {
        "task_id": reservation.task_id,
        "previous_attempt_id": str(attempt_id),
        "retry_key": "retry-key-1",
        "config_snapshot": {
            "capability": "script.analysis",
            "parameters": {},
            "metering_formula": {
                "kind": "llm",
                "input_weight": 1,
                "output_weight": 2,
                "max_input_tokens": 1000,
                "max_output_tokens": 500,
            },
        },
        "provider": "dashscope",
        "provider_model_id": "qwen-backup",
        "maximum_metering_tokens": 2000,
    }
    values.update(updates)
    return service.reserve_resubmission(context, **values)


def test_proven_nonbillable_resumption_reuses_existing_hold() -> None:
    database, context, reservation, attempt_id = _retry_database()

    result = RetryAccountingService(database).reuse_proven_nonbillable_hold(
        context,
        task_id=reservation.task_id,
        attempt_id=str(attempt_id),
    )

    assert result.reused_existing_hold is True
    assert result.hold_id == reservation.hold_id
    assert result.attempt_id == str(attempt_id)
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 3
    database.engine.dispose()


def test_unproven_attempt_cannot_reuse_existing_hold() -> None:
    database, context, reservation, attempt_id = _retry_database(
        attempt_status="ambiguous",
        billing_state=None,
        provider_task_id="provider-task-1",
    )

    with pytest.raises(RetryAccountingConflictError, match="无法证明"):
        RetryAccountingService(database).reuse_proven_nonbillable_hold(
            context,
            task_id=reservation.task_id,
            attempt_id=str(attempt_id),
        )
    database.engine.dispose()


def test_resubmission_releases_proven_nonbillable_hold_and_reserves_new_attempt() -> None:
    database, context, reservation, attempt_id = _retry_database()
    service = RetryAccountingService(database)

    result = _resubmit(service, context, reservation, attempt_id)

    assert result.attempt_number == 2
    assert result.released_previous_microtickets == 2_000_000
    assert result.quoted_microtickets == 2_000_000
    assert result.wallet.available_microtickets == 3_000_000
    assert result.wallet.held_microtickets == 2_000_000
    with database.session_factory() as session:
        previous_hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        new_hold = session.get(TicketHoldRecord, int(result.hold_id))
        attempt = session.get(AITaskAttemptRecord, int(result.attempt_id))
        assert previous_hold is not None
        assert new_hold is not None
        assert attempt is not None
        assert previous_hold.status == "released"
        assert new_hold.status == "held"
        assert new_hold.attempt_id == attempt.id
        assert attempt.retry_of_attempt_id == attempt_id
        assert attempt.retry_key == "retry-key-1"
    database.engine.dispose()


def test_potentially_billable_resubmission_keeps_old_hold_and_reserves_again() -> None:
    database, context, reservation, attempt_id = _retry_database(
        attempt_status="ambiguous",
        billing_state=None,
        provider_task_id="provider-task-ambiguous",
    )

    result = _resubmit(
        RetryAccountingService(database),
        context,
        reservation,
        attempt_id,
    )

    assert result.released_previous_microtickets == 0
    assert result.wallet.available_microtickets == 1_000_000
    assert result.wallet.held_microtickets == 4_000_000
    with database.session_factory() as session:
        old_hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        assert old_hold is not None
        assert old_hold.status == "held"
        assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 2
    database.engine.dispose()


def test_potentially_billable_resubmission_requires_additional_balance() -> None:
    database, context, reservation, attempt_id = _retry_database(
        initial_balance=3_000_000,
        attempt_status="ambiguous",
        billing_state=None,
        provider_task_id="provider-task-ambiguous",
    )

    with pytest.raises(InsufficientTicketBalanceError):
        _resubmit(
            RetryAccountingService(database),
            context,
            reservation,
            attempt_id,
        )

    with database.session_factory() as session:
        old_hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        assert old_hold is not None
        assert old_hold.status == "held"
        assert old_hold.remaining_microtickets == 2_000_000
        assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
        assert session.scalar(select(func.count()).select_from(AITaskAttemptRecord)) == 1
    database.engine.dispose()


def test_resubmission_retry_key_is_idempotent() -> None:
    database, context, reservation, attempt_id = _retry_database()
    service = RetryAccountingService(database)
    first = _resubmit(service, context, reservation, attempt_id)

    second = _resubmit(
        service,
        context,
        reservation,
        attempt_id,
        config_snapshot={"changed": object()},
        maximum_metering_tokens=-1,
    )

    assert second.reused_retry is True
    assert second.attempt_id == first.attempt_id
    assert second.hold_id == first.hold_id
    assert second.wallet.version == first.wallet.version
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 2
        assert session.scalar(select(func.count()).select_from(AITaskAttemptRecord)) == 2
    database.engine.dispose()


def test_resubmitted_attempt_settles_against_its_own_snapshot_and_hold() -> None:
    database, context, reservation, attempt_id = _retry_database()
    retry = _resubmit(
        RetryAccountingService(database),
        context,
        reservation,
        attempt_id,
    )
    with database.session_factory.begin() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        assert task is not None
        task.status = "provider_succeeded"

    settlement = TicketSettlementService(database).settle_success(
        context,
        task_id=reservation.task_id,
        attempt_id=retry.attempt_id,
        raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
    )

    assert settlement.attempt_id == retry.attempt_id
    assert settlement.charged_microtickets == 200_000
    assert settlement.released_microtickets == 1_800_000
    assert settlement.wallet.available_microtickets == 4_800_000
    assert settlement.wallet.held_microtickets == 0
    with database.session_factory() as session:
        usage = session.get(UsageEventRecord, int(settlement.usage_event_id))
        new_hold = session.get(TicketHoldRecord, int(retry.hold_id))
        old_hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        assert usage is not None
        assert new_hold is not None
        assert old_hold is not None
        assert usage.attempt_id == int(retry.attempt_id)
        assert new_hold.status == "settled"
        assert old_hold.status == "released"
    database.engine.dispose()
