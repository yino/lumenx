from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.platform.auth.admin import AdminAuthorizationError
from src.platform.auth.sessions import SessionPrincipal
from src.platform.db_models import (
    AuditEventRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
)
from src.platform.ticket_administration_api import (
    install_cloud_ticket_administration_api,
)
from src.platform.ticket_wallet import TicketWalletService
from tests.test_content_repositories import RepositoryDatabase, _create_scope


@pytest.fixture
def ticket_admin_client():
    database = RepositoryDatabase()
    TicketWalletRecord.__table__.create(database.engine)
    TicketLedgerRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    admin_context = _create_scope(database)
    target_context = _create_scope(database)
    with database.transaction(target_context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            target_context.identity.user_id,
            1_000_000,
            reason="创建目标钱包",
        )
    sessions = Mock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=int(admin_context.identity.user_id),
        session_id=int(admin_context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
        is_platform_admin=True,
    )
    app = FastAPI()
    install_cloud_ticket_administration_api(
        app,
        SimpleNamespace(database=database, sessions=sessions),
    )

    @app.exception_handler(AdminAuthorizationError)
    def handle_admin_denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(
            status_code=403,
            content={"code": "ADMIN_REQUIRED", "message": str(exc)},
        )

    client = TestClient(app)
    client.cookies.set("lumenx_session", "admin-session")
    yield client, sessions, database, target_context
    database.engine.dispose()


def test_admin_ticket_api_grants_debits_compensates_and_lists_audit_history(
    ticket_admin_client,
) -> None:
    client, _sessions, database, target = ticket_admin_client
    user_id = target.identity.user_id

    granted = client.post(
        f"/admin/tickets/users/{user_id}/grant",
        json={"amount_tickets": "1.25", "reason": "新用户活动赠送"},
    )
    debited = client.post(
        f"/admin/tickets/users/{user_id}/debit",
        json={"amount_tickets": "0.5", "reason": "纠正重复赠送"},
    )
    compensated = client.post(
        f"/admin/tickets/users/{user_id}/compensate",
        json={"amount_tickets": "0.125", "reason": "任务异常补偿"},
    )
    history = client.get(f"/admin/tickets/users/{user_id}")

    assert granted.status_code == 200
    assert debited.status_code == 200
    assert compensated.status_code == 200
    assert compensated.json()["wallet"]["available_tickets"] == "1.875"
    assert history.status_code == 200
    assert history.json()["wallet"]["held_tickets"] == "0"
    assert history.json()["total"] == 4
    assert {item["entry_type"] for item in history.json()["ledger"]} == {
        "compensation",
        "adjustment",
        "grant",
    }
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuditEventRecord)) == 3
        actions = set(session.scalars(select(AuditEventRecord.action)))
        assert actions == {"ticket.grant", "ticket.debit", "ticket.compensation"}


def test_admin_ticket_api_rejects_excessive_debit_without_partial_history(
    ticket_admin_client,
) -> None:
    client, _sessions, database, target = ticket_admin_client

    response = client.post(
        f"/admin/tickets/users/{target.identity.user_id}/debit",
        json={"amount_tickets": "1.000001", "reason": "超额扣减"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "TICKET_ADJUSTMENT_CONFLICT"
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEventRecord)) == 0


def test_ticket_administration_denies_normal_user(ticket_admin_client) -> None:
    client, sessions, _database, target = ticket_admin_client
    sessions.resolve.return_value = replace(
        sessions.resolve.return_value,
        is_platform_admin=False,
    )

    response = client.get(f"/admin/tickets/users/{target.identity.user_id}")

    assert response.status_code == 403
    assert response.json() == {
        "code": "ADMIN_REQUIRED",
        "message": "仅平台管理员可以管理用户算力券",
    }


def test_ticket_adjustment_requires_positive_six_decimal_amount(
    ticket_admin_client,
) -> None:
    client, _sessions, _database, target = ticket_admin_client
    endpoint = f"/admin/tickets/users/{target.identity.user_id}/grant"

    assert client.post(
        endpoint,
        json={"amount_tickets": "0", "reason": "无效"},
    ).status_code == 422
    assert client.post(
        endpoint,
        json={"amount_tickets": "0.0000001", "reason": "精度过高"},
    ).status_code == 422
    assert client.post(
        endpoint,
        json={"amount_tickets": "1", "reason": ""},
    ).status_code == 422
