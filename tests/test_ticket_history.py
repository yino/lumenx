from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.platform.auth.sessions import SessionPrincipal
from src.platform.db_models import TicketLedgerRecord
from src.platform.ticket_history_api import install_cloud_ticket_history_api
from tests.test_content_repositories import _create_scope
from tests.test_ticket_settlement import _settlement_database


@pytest.fixture
def ticket_history_client():
    database, context, reservation = _settlement_database()
    from src.platform.ticket_settlement import TicketSettlementService

    TicketSettlementService(database).settle_success(
        context,
        task_id=reservation.task_id,
        raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
    )
    sessions = Mock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
        is_platform_admin=False,
    )
    app = FastAPI()
    install_cloud_ticket_history_api(
        app,
        SimpleNamespace(database=database, sessions=sessions),
    )
    client = TestClient(app)
    client.cookies.set("lumenx_session", "user-session")
    yield client, sessions, database, context
    database.engine.dispose()


def test_user_wallet_summary_and_paginated_ledger_usage_views(
    ticket_history_client,
) -> None:
    client, _sessions, _database, context = ticket_history_client

    summary = client.get("/wallet")
    ledger = client.get("/wallet/history", params={"view": "ledger", "limit": 2})
    usage = client.get("/wallet/history", params={"view": "usage", "limit": 20})

    assert summary.status_code == 200
    assert summary.json()["available_tickets"] == "2.8"
    assert summary.json()["held_tickets"] == "0"
    assert ledger.status_code == 200
    assert ledger.json()["total"] == 4
    assert ledger.json()["limit"] == 2
    assert len(ledger.json()["items"]) == 2
    first_ledger_item = ledger.json()["items"][0]
    expected_available = format(
        Decimal(first_ledger_item["available_after"]) / Decimal(1_000_000),
        "f",
    ).rstrip("0").rstrip(".") or "0"
    assert first_ledger_item["available_after_tickets"] == expected_available
    assert {item["status_zh"] for item in ledger.json()["items"]} <= {
        "已结算",
        "已释放",
        "已入账",
    }
    assert usage.status_code == 200
    assert usage.json()["total"] == 1
    item = usage.json()["items"][0]
    assert item["workspace_id"] == context.workspace_id
    assert item["metering_tokens"] == "200"
    assert item["charged_tickets"] == "0.2"
    assert item["status_zh"] == "已完成"


def test_wallet_history_never_accepts_foreign_user_scope(ticket_history_client) -> None:
    client, sessions, database, _context = ticket_history_client
    foreign_context = _create_scope(database)
    sessions.resolve.return_value = SessionPrincipal(
        user_id=int(foreign_context.identity.user_id),
        session_id=int(foreign_context.identity.session_id),
        phone_canonical="+8613900139000",
        phone_verified=False,
        is_platform_admin=False,
    )

    response = client.get("/wallet/history", params={"view": "usage"})

    assert response.status_code == 404
    assert response.json()["code"] == "TICKET_WALLET_NOT_FOUND"
    assert b"user_id" not in response.request.url.query


def test_wallet_history_validates_pagination(ticket_history_client) -> None:
    client, _sessions, _database, _context = ticket_history_client

    assert client.get("/wallet/history", params={"offset": -1}).status_code == 422
    assert client.get("/wallet/history", params={"limit": 101}).status_code == 422
    assert client.get("/wallet/history", params={"view": "other"}).status_code == 422


def test_wallet_history_ignores_invalid_usage_correlation(ticket_history_client) -> None:
    client, _sessions, database, context = ticket_history_client
    with database.transaction(context.identity) as session:
        record = session.scalar(
            select(TicketLedgerRecord).where(
                TicketLedgerRecord.user_id == int(context.identity.user_id),
                TicketLedgerRecord.entry_type == "settlement",
            )
        )
        assert record is not None
        record.correlation = {"usage_event_id": "invalid-history-id"}

    response = client.get("/wallet/history", params={"view": "ledger"})

    assert response.status_code == 200
    settlement = next(
        item for item in response.json()["items"] if item["entry_type"] == "settlement"
    )
    assert settlement["metering_tokens"] is None
