from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.platform.contracts import AdminContext
from sqlalchemy import func, select

from src.platform.db_models import (
    AITaskRecord,
    AuditEventRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
)
from src.platform.ticket_administration import TicketAdministrationService
from src.platform.ticket_reconciliation import (
    TicketReconciliationAuthorizationError,
    TicketReconciliationService,
    TicketReconciliationValidationError,
)
from src.platform.ticket_settlement import TicketSettlementService
from tests.test_ticket_settlement import _settlement_database


def _admin(_context) -> AdminContext:
    return AdminContext(
        admin_id="9001",
        session_id="reconciliation-test",
        username="admin",
    )


def test_reconciliation_reports_consistent_settled_wallet() -> None:
    database, context, reservation = _settlement_database()
    try:
        with database.transaction(context.identity) as session:
            task = session.get(AITaskRecord, int(reservation.task_id))
            assert task is not None
            task.status = "provider_succeeded"
        TicketSettlementService(database).settle_success(
            context,
            task_id=reservation.task_id,
            raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
        )

        report = TicketReconciliationService(database).reconcile(_admin(context))

        assert report.is_consistent is True
        assert report.wallets_scanned == 1
        assert report.holds_scanned == 1
        assert report.usage_events_scanned == 1
        assert report.issues == ()
    finally:
        database.engine.dispose()


def test_reconciliation_reports_invariants_without_mutating_history() -> None:
    database, context, reservation = _settlement_database()
    future_now = datetime.now(timezone.utc) + timedelta(days=2)
    try:
        with database.transaction(context.identity) as session:
            task = session.get(AITaskRecord, int(reservation.task_id))
            hold = session.get(TicketHoldRecord, int(reservation.hold_id))
            wallet = session.get(
                TicketWalletRecord,
                int(context.identity.user_id),
            )
            assert task is not None and hold is not None and wallet is not None
            task.status = "failed"
            task.completed_at = datetime.now(timezone.utc)
            wallet.available_microtickets += 1
            before_wallet = (
                wallet.available_microtickets,
                wallet.held_microtickets,
                wallet.version,
            )
            before_hold = (hold.status, hold.remaining_microtickets)

        report = TicketReconciliationService(database).reconcile(
            _admin(context),
            now=future_now,
            stale_after=timedelta(hours=24),
        )

        codes = {issue.code for issue in report.issues}
        assert report.is_consistent is False
        assert "WALLET_LEDGER_CACHE_MISMATCH" in codes
        assert "TERMINAL_TASK_OPEN_HOLD" in codes
        assert "STALE_OPEN_HOLD" in codes
        assert "TERMINAL_TASK_USAGE_MISSING" in codes

        with database.transaction(context.identity) as session:
            wallet = session.get(
                TicketWalletRecord,
                int(context.identity.user_id),
            )
            hold = session.get(TicketHoldRecord, int(reservation.hold_id))
            assert wallet is not None and hold is not None
            assert (
                wallet.available_microtickets,
                wallet.held_microtickets,
                wallet.version,
            ) == before_wallet
            assert (hold.status, hold.remaining_microtickets) == before_hold
    finally:
        database.engine.dispose()


def test_reconciliation_requires_admin_and_valid_limits() -> None:
    database, context, _reservation = _settlement_database()
    service = TicketReconciliationService(database)
    try:
        with pytest.raises(TicketReconciliationAuthorizationError):
            service.reconcile(context.identity)
        with pytest.raises(TicketReconciliationValidationError):
            service.reconcile(_admin(context), stale_after=timedelta(0))
        with pytest.raises(TicketReconciliationValidationError):
            service.reconcile(_admin(context), max_issues=0)
    finally:
        database.engine.dispose()


def test_compensation_appends_correction_and_preserves_original_ledger() -> None:
    database, context, reservation = _settlement_database()
    AuditEventRecord.__table__.create(database.engine)
    try:
        settlement = TicketSettlementService(database).settle_success(
            context,
            task_id=reservation.task_id,
            raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
        )
        with database.session_factory() as session:
            original = session.scalar(
                select(TicketLedgerRecord).where(
                    TicketLedgerRecord.entry_type == "settlement"
                )
            )
            assert original is not None
            original_id = original.id
            original_values = (
                original.amount_microtickets,
                original.available_delta,
                original.held_delta,
                dict(original.correlation),
            )

        TicketAdministrationService(database).adjust(
            _admin(context),
            int(context.identity.user_id),
            operation="compensation",
            amount_microtickets=settlement.charged_microtickets,
            reason="纠正已确认的异常扣费",
            correlation_id=str(original_id),
        )

        with database.session_factory() as session:
            original = session.get(TicketLedgerRecord, original_id)
            assert original is not None
            assert (
                original.amount_microtickets,
                original.available_delta,
                original.held_delta,
                dict(original.correlation),
            ) == original_values
            compensation = session.scalar(
                select(TicketLedgerRecord).where(
                    TicketLedgerRecord.entry_type == "compensation"
                )
            )
            assert compensation is not None
            assert compensation.id != original_id
            assert compensation.correlation["correlation_id"] == str(original_id)
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 5

        report = TicketReconciliationService(database).reconcile(_admin(context))
        assert report.is_consistent is True
    finally:
        database.engine.dispose()
