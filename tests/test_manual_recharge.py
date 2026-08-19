from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.platform.auth.admin import AdminAuthorizationError
from src.platform.auth.admin_identity import AdminSessionPrincipal
from src.platform.auth.sessions import SessionAuthenticationError
from src.platform.contracts import AdminContext
from src.platform.db_models import (
    AuditEventRecord,
    ManualRechargeOrderEventRecord,
    ManualRechargeOrderRecord,
    ManualRechargeReconciliationReportRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
)
from src.platform.manual_recharge import (
    ManualRechargeConflictError,
    ManualRechargeOrderService,
)
from src.platform.manual_recharge_api import install_cloud_manual_recharge_api
from src.platform.settings import DeploymentSettings
from tests.test_content_repositories import RepositoryDatabase, _create_scope


class ManualRechargeDatabase(RepositoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        TicketWalletRecord.__table__.create(self.engine)
        TicketLedgerRecord.__table__.create(self.engine)
        AuditEventRecord.__table__.create(self.engine)
        ManualRechargeOrderRecord.__table__.create(self.engine)
        ManualRechargeOrderEventRecord.__table__.create(self.engine)
        ManualRechargeReconciliationReportRecord.__table__.create(self.engine)


@pytest.fixture
def recharge_scope():
    database = ManualRechargeDatabase()
    target = _create_scope(database)
    admin = AdminContext(admin_id="9001", session_id="9101", username="admin")
    with database.session_factory.begin() as session:
        session.add(
            TicketWalletRecord(
                user_id=int(target.identity.user_id),
                available_microtickets=2_000_000,
                held_microtickets=500_000,
                lifetime_granted_microtickets=2_000_000,
            )
        )
    yield database, target, admin
    database.engine.dispose()


def _create_order(service, admin, user_id, *, key="create-key", reference="OFFLINE-001"):
    return service.create_pending(
        admin,
        user_id=user_id,
        cash_amount_fen=1999,
        ticket_amount_microtickets=3_000_000,
        offline_reference=reference,
        reason="客服确认用户线下购买",
        idempotency_key=key,
        exchange_snapshot={"tokens_per_ticket": 1000, "config_version_id": "1"},
    )


def test_pending_complete_and_refund_are_atomic_and_idempotent(recharge_scope) -> None:
    database, target, admin = recharge_scope
    service = ManualRechargeOrderService(database)
    user_id = int(target.identity.user_id)

    pending = _create_order(service, admin, user_id)
    replayed_create = _create_order(service, admin, user_id)
    with database.session_factory() as session:
        wallet = session.scalar(select(TicketWalletRecord).where(TicketWalletRecord.user_id == user_id))
        assert wallet.available_microtickets == 2_000_000
        assert pending.id == replayed_create.id

    completed = service.complete(
        admin,
        pending.id,
        expected_version=1,
        reason="财务人工确认线下款项到账",
        idempotency_key="complete-key",
    )
    replayed_complete = service.complete(
        admin,
        pending.id,
        expected_version=1,
        reason="财务人工确认线下款项到账",
        idempotency_key="complete-key",
    )
    assert completed.status == "completed"
    assert replayed_complete.id == completed.id

    partial = service.refund(
        admin,
        pending.id,
        cash_amount_fen=999,
        ticket_amount_microtickets=1_000_000,
        expected_version=2,
        reason="客服登记用户部分退款",
        idempotency_key="refund-key-1",
    )
    assert partial.status == "partially_refunded"
    final = service.refund(
        admin,
        pending.id,
        cash_amount_fen=1000,
        ticket_amount_microtickets=2_000_000,
        expected_version=3,
        reason="客服登记用户剩余退款",
        idempotency_key="refund-key-2",
    )
    assert final.status == "refunded"

    with database.session_factory() as session:
        wallet = session.scalar(select(TicketWalletRecord).where(TicketWalletRecord.user_id == user_id))
        assert wallet.available_microtickets == 2_000_000
        assert wallet.held_microtickets == 500_000
        assert wallet.lifetime_recharged_microtickets == 3_000_000
        assert wallet.lifetime_refunded_microtickets == 3_000_000
        assert session.scalar(
            select(func.count()).select_from(TicketLedgerRecord).where(
                TicketLedgerRecord.manual_recharge_order_id == pending.id
            )
        ) == 3
        assert session.scalar(
            select(func.count()).select_from(ManualRechargeOrderEventRecord).where(
                ManualRechargeOrderEventRecord.order_id == pending.id
            )
        ) == 4


def test_invalid_transitions_versions_references_and_refund_balance_are_rejected(
    recharge_scope,
) -> None:
    database, target, admin = recharge_scope
    service = ManualRechargeOrderService(database)
    user_id = int(target.identity.user_id)
    order = _create_order(service, admin, user_id)

    with pytest.raises(ManualRechargeConflictError) as reused:
        service.create_pending(
            admin,
            user_id=user_id,
            cash_amount_fen=2000,
            ticket_amount_microtickets=3_000_000,
            offline_reference="OFFLINE-002",
            reason="客服登记另一笔充值",
            idempotency_key="create-key",
            exchange_snapshot={"tokens_per_ticket": 1000, "config_version_id": "1"},
        )
    assert reused.value.code == "IDEMPOTENCY_FINGERPRINT_CONFLICT"
    with pytest.raises(ManualRechargeConflictError) as duplicate_reference:
        _create_order(service, admin, user_id, key="create-key-2", reference="offline-001")
    assert duplicate_reference.value.code == "OFFLINE_REFERENCE_DUPLICATE"
    with pytest.raises(ManualRechargeConflictError) as stale:
        service.complete(
            admin,
            order.id,
            expected_version=9,
            reason="财务确认线下到账",
            idempotency_key="stale-complete",
        )
    assert stale.value.code == "ORDER_VERSION_CONFLICT"

    service.complete(
        admin,
        order.id,
        expected_version=1,
        reason="财务确认线下到账",
        idempotency_key="complete",
    )
    with pytest.raises(ManualRechargeConflictError, match="退款"):
        service.cancel(
            admin,
            order.id,
            expected_version=2,
            reason="客服尝试取消已完成订单",
            idempotency_key="cancel-completed",
        )
    with database.session_factory.begin() as session:
        wallet = session.scalar(select(TicketWalletRecord).where(TicketWalletRecord.user_id == user_id))
        wallet.available_microtickets = 1
    with pytest.raises(ManualRechargeConflictError) as shortfall:
        service.refund(
            admin,
            order.id,
            cash_amount_fen=1999,
            ticket_amount_microtickets=3_000_000,
            expected_version=2,
            reason="客服登记全额退款",
            idempotency_key="refund-shortfall",
        )
    assert shortfall.value.code == "REFUND_BALANCE_SHORTFALL"
    assert shortfall.value.details["shortfall_microtickets"] == 2_999_999


def test_cancel_does_not_mutate_wallet_and_reconciliation_persists_report(recharge_scope) -> None:
    database, target, admin = recharge_scope
    service = ManualRechargeOrderService(database)
    user_id = int(target.identity.user_id)
    order = _create_order(service, admin, user_id)
    cancelled = service.cancel(
        admin,
        order.id,
        expected_version=1,
        reason="客服确认用户取消线下购买",
        idempotency_key="cancel",
    )
    assert cancelled.status == "cancelled"
    report = service.reconcile_order(
        admin,
        order.id,
        reason="财务执行人工订单对账",
    )
    assert report.status == "consistent"
    with database.session_factory() as session:
        wallet = session.scalar(select(TicketWalletRecord).where(TicketWalletRecord.user_id == user_id))
        assert wallet.available_microtickets == 2_000_000
        assert session.get(ManualRechargeReconciliationReportRecord, report.report_id) is not None


def test_manual_recharge_api_requires_admin_csrf_idempotency_and_returns_safe_errors(
    recharge_scope,
) -> None:
    database, target, _admin = recharge_scope
    sessions = Mock()
    sessions.resolve.return_value = AdminSessionPrincipal(
        admin_id=9001,
        session_id=9101,
        username="admin",
        must_change_password=False,
    )
    runtime_policy = Mock()
    runtime_policy.resolve.return_value = SimpleNamespace(
        config_version_id="1",
        tokens_per_ticket=1000,
    )
    app = FastAPI()
    install_cloud_manual_recharge_api(
        app,
        SimpleNamespace(
            database=database,
            admin_sessions=sessions,
            runtime_policy=runtime_policy,
        ),
        DeploymentSettings(_env_file=None),
    )

    @app.exception_handler(AdminAuthorizationError)
    def denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(status_code=403, content={"code": "ADMIN_REQUIRED", "message": str(exc)})

    @app.exception_handler(SessionAuthenticationError)
    def unauthenticated(_request: Request, exc: SessionAuthenticationError):
        return JSONResponse(status_code=401, content={"code": exc.code, "message": str(exc)})

    client = TestClient(app)
    client.cookies.set("lumenx_admin_session", "admin-session")
    response = client.post(
        "/admin/recharge-orders",
        headers={"x-csrf-token": "csrf", "idempotency-key": "api-create"},
        json={
            "user_id": int(target.identity.user_id),
            "cash_amount_fen": 1000,
            "ticket_amount_microtickets": 1_000_000,
            "offline_reference": "API-OFFLINE-1",
            "reason": "客服登记线下充值订单",
        },
    )
    assert response.status_code == 201
    assert response.json()["status_zh"] == "待确认"
    sessions.resolve.assert_called_with("admin-session", csrf_token="csrf")

    stale_response = client.post(
        f"/admin/recharge-orders/{response.json()['id']}/complete",
        headers={"x-csrf-token": "csrf", "idempotency-key": "api-stale-complete"},
        json={
            "expected_version": 9,
            "reason": "财务使用过期版本确认到账",
        },
    )
    assert stale_response.status_code == 409
    assert stale_response.json()["code"] == "ORDER_VERSION_CONFLICT"
    assert stale_response.json()["details"] == {
        "current_version": 1,
        "current_status": "pending",
    }

    client.cookies.delete("lumenx_admin_session")
    client.cookies.set("lumenx_session", "normal-user-session")
    denied_response = client.get("/admin/recharge-orders")
    assert denied_response.status_code == 401
    assert "API-OFFLINE-1" not in denied_response.text


@pytest.mark.parametrize(
    ("cash", "tickets", "expected_code"),
    [
        (0, 1, "AMOUNT_INVALID"),
        (-1, 1, "AMOUNT_INVALID"),
        (True, 1, "AMOUNT_INVALID"),
        (1, False, "AMOUNT_INVALID"),
        (ManualRechargeOrderService.MAX_CASH_AMOUNT_FEN + 1, 1, "AMOUNT_LIMIT_EXCEEDED"),
        (1, ManualRechargeOrderService.MAX_TICKET_AMOUNT_MICROTICKETS + 1, "AMOUNT_LIMIT_EXCEEDED"),
    ],
)
def test_amount_and_reason_boundaries_are_enforced_in_domain_service(
    recharge_scope,
    cash,
    tickets,
    expected_code,
) -> None:
    database, target, admin = recharge_scope
    service = ManualRechargeOrderService(database)
    with pytest.raises(ManualRechargeConflictError) as captured:
        service.create_pending(
            admin,
            user_id=int(target.identity.user_id),
            cash_amount_fen=cash,
            ticket_amount_microtickets=tickets,
            offline_reference=None,
            reason="客服登记金额边界测试",
            idempotency_key=f"boundary-{cash}-{tickets}",
            exchange_snapshot={"tokens_per_ticket": 1000},
        )
    assert captured.value.code == expected_code

    with pytest.raises(ManualRechargeConflictError) as invalid_reason:
        service.create_pending(
            admin,
            user_id=int(target.identity.user_id),
            cash_amount_fen=1,
            ticket_amount_microtickets=1,
            offline_reference=None,
            reason="support only",
            idempotency_key="invalid-reason",
            exchange_snapshot={"tokens_per_ticket": 1000},
        )
    assert invalid_reason.value.code == "REASON_INVALID"


def test_maximum_amount_boundary_is_accepted(recharge_scope) -> None:
    database, target, admin = recharge_scope
    order = ManualRechargeOrderService(database).create_pending(
        admin,
        user_id=int(target.identity.user_id),
        cash_amount_fen=ManualRechargeOrderService.MAX_CASH_AMOUNT_FEN,
        ticket_amount_microtickets=ManualRechargeOrderService.MAX_TICKET_AMOUNT_MICROTICKETS,
        offline_reference="BOUNDARY-MAX",
        reason="客服登记最大允许金额测试",
        idempotency_key="boundary-max",
        exchange_snapshot={"tokens_per_ticket": 1000},
    )
    assert order.cash_amount_fen == ManualRechargeOrderService.MAX_CASH_AMOUNT_FEN
    assert order.ticket_amount_microtickets == ManualRechargeOrderService.MAX_TICKET_AMOUNT_MICROTICKETS


def test_audit_failure_rolls_back_order_wallet_event_and_ledger(recharge_scope, monkeypatch) -> None:
    database, target, admin = recharge_scope
    service = ManualRechargeOrderService(database)
    user_id = int(target.identity.user_id)
    order = _create_order(service, admin, user_id)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service, "_append_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.complete(
            admin,
            order.id,
            expected_version=1,
            reason="财务确认线下到账",
            idempotency_key="complete-audit-failure",
        )

    with database.session_factory() as session:
        persisted = session.get(ManualRechargeOrderRecord, order.id)
        wallet = session.scalar(select(TicketWalletRecord).where(TicketWalletRecord.user_id == user_id))
        assert persisted.status == "pending"
        assert persisted.version == 1
        assert wallet.available_microtickets == 2_000_000
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ManualRechargeOrderEventRecord)) == 1


def test_each_transition_rejects_idempotency_fingerprint_reuse(recharge_scope) -> None:
    database, target, admin = recharge_scope
    service = ManualRechargeOrderService(database)
    order = _create_order(service, admin, int(target.identity.user_id))
    service.complete(admin, order.id, expected_version=1, reason="财务确认线下到账", idempotency_key="same-complete")
    with pytest.raises(ManualRechargeConflictError) as complete_conflict:
        service.complete(admin, order.id, expected_version=1, reason="财务以不同原因重试", idempotency_key="same-complete")
    assert complete_conflict.value.code == "IDEMPOTENCY_FINGERPRINT_CONFLICT"

    service.refund(
        admin,
        order.id,
        cash_amount_fen=500,
        ticket_amount_microtickets=500_000,
        expected_version=2,
        reason="客服登记部分退款",
        idempotency_key="same-refund",
    )
    with pytest.raises(ManualRechargeConflictError) as refund_conflict:
        service.refund(
            admin,
            order.id,
            cash_amount_fen=500,
            ticket_amount_microtickets=400_000,
            expected_version=2,
            reason="客服登记部分退款",
            idempotency_key="same-refund",
        )
    assert refund_conflict.value.code == "IDEMPOTENCY_FINGERPRINT_CONFLICT"


def test_reconciliation_reports_mismatch_without_repairing_financial_history(recharge_scope) -> None:
    database, target, admin = recharge_scope
    service = ManualRechargeOrderService(database)
    order = _create_order(service, admin, int(target.identity.user_id))
    service.complete(admin, order.id, expected_version=1, reason="财务确认线下到账", idempotency_key="complete")
    with database.session_factory.begin() as session:
        ledger = session.scalar(select(TicketLedgerRecord).where(TicketLedgerRecord.manual_recharge_order_id == order.id))
        session.delete(ledger)

    result = service.reconcile_order(admin, order.id, reason="财务核查订单账本差异")
    assert result.status == "mismatch"
    assert "充值账本与订单算力券不一致" in result.details["mismatches"]
    with database.session_factory() as session:
        persisted = session.get(ManualRechargeOrderRecord, order.id)
        wallet = session.scalar(select(TicketWalletRecord).where(TicketWalletRecord.user_id == order.user_id))
        assert persisted.status == "completed"
        assert persisted.version == 2
        assert wallet.available_microtickets == 5_000_000
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 0
