from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from src.platform.db_models import (
    AITaskRecord,
    ProjectRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
)
from src.platform.ticket_reservation import (
    InsufficientTicketBalanceError,
    TicketReservationConflictError,
    TicketReservationScopeNotFoundError,
    TicketReservationService,
)
from src.platform.ticket_wallet import TicketWalletService
from tests.test_content_repositories import RepositoryDatabase, _create_scope, _script


def _reservation_database(initial_balance: int = 2_000_000):
    database = RepositoryDatabase()
    TicketWalletRecord.__table__.create(database.engine)
    AITaskRecord.__table__.create(database.engine)
    TicketHoldRecord.__table__.create(database.engine)
    TicketLedgerRecord.__table__.create(database.engine)
    context = _create_scope(database)
    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            initial_balance,
            reason="创建测试钱包",
        )
    return database, context


def _reserve(service, context, **updates):
    values = {
        "capability": "image.t2i",
        "idempotency_key": "task-key-1",
        "request_payload": {
            "prompt": "雨夜中的城市",
            "parameters": {"count": 1, "resolution": "1024x1024"},
        },
        "config_snapshot": {
            "config_version_id": "1",
            "provider": "dashscope",
            "provider_model_id": "wanx-v1",
        },
        "maximum_metering_tokens": 1500,
        "tokens_per_ticket": 1000,
    }
    values.update(updates)
    return service.reserve_task(context, **values)


def test_reservation_atomically_creates_task_hold_ledger_and_wallet_cache() -> None:
    database, context = _reservation_database()
    service = TicketReservationService(database)

    result = _reserve(service, context)

    assert result.reused is False
    assert result.status == "reserved"
    assert result.quoted_microtickets == 1_500_000
    assert result.wallet.available_microtickets == 500_000
    assert result.wallet.held_microtickets == 1_500_000
    assert result.wallet.version == 2
    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(result.task_id))
        hold = session.get(TicketHoldRecord, int(result.hold_id))
        assert task is not None
        assert hold is not None
        assert task.request_payload["prompt"] == "雨夜中的城市"
        assert task.tokens_per_ticket == 1000
        assert hold.task_id == task.id
        assert hold.remaining_microtickets == 1_500_000
        ledger = session.scalar(
            select(TicketLedgerRecord).where(TicketLedgerRecord.entry_type == "hold")
        )
        assert ledger is not None
        assert ledger.task_id == task.id
        assert ledger.hold_id == hold.id
    database.engine.dispose()


def test_same_idempotency_key_and_request_returns_original_reservation() -> None:
    database, context = _reservation_database()
    service = TicketReservationService(database)
    first = _reserve(service, context)

    second = _reserve(
        service,
        context,
        config_snapshot={"config_version_id": "2"},
        maximum_metering_tokens=999_999,
        tokens_per_ticket=1,
    )

    assert second.reused is True
    assert second.task_id == first.task_id
    assert second.hold_id == first.hold_id
    assert second.quoted_microtickets == first.quoted_microtickets
    assert second.tokens_per_ticket == first.tokens_per_ticket
    assert second.wallet.version == 2
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
        assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 2
    database.engine.dispose()


def test_reusing_idempotency_key_for_different_request_is_rejected() -> None:
    database, context = _reservation_database()
    service = TicketReservationService(database)
    _reserve(service, context)

    with pytest.raises(TicketReservationConflictError, match="不同的 AI 请求"):
        _reserve(
            service,
            context,
            request_payload={"prompt": "不同内容", "parameters": {"count": 1}},
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
        wallet = session.scalar(
            select(TicketWalletRecord).where(
                TicketWalletRecord.user_id == int(context.identity.user_id)
            )
        )
        assert wallet is not None
        assert wallet.available_microtickets == 500_000
        assert wallet.held_microtickets == 1_500_000
    database.engine.dispose()


def test_insufficient_balance_rolls_back_task_hold_and_reservation_ledger() -> None:
    database, context = _reservation_database(initial_balance=1_499_999)
    service = TicketReservationService(database)

    with pytest.raises(InsufficientTicketBalanceError) as captured:
        _reserve(service, context)

    assert captured.value.available_microtickets == 1_499_999
    assert captured.value.required_microtickets == 1_500_000
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
        wallet = session.scalar(
            select(TicketWalletRecord).where(
                TicketWalletRecord.user_id == int(context.identity.user_id)
            )
        )
        assert wallet is not None
        assert wallet.available_microtickets == 1_499_999
        assert wallet.held_microtickets == 0
        assert wallet.version == 1
    database.engine.dispose()


def test_foreign_project_scope_is_rejected_before_reservation() -> None:
    database, context = _reservation_database()
    foreign_context = _create_scope(database)
    foreign_project = _script()
    with database.transaction(foreign_context.identity) as session:
        record = ProjectRecord(
            user_id=int(foreign_context.identity.user_id),
            workspace_id=int(foreign_context.workspace_id),
            title=foreign_project.title,
            payload=foreign_project.model_dump(mode="json"),
        )
        session.add(record)
        session.flush()
        record.payload = {
            **foreign_project.model_dump(mode="json"),
            "id": str(record.id),
        }
        foreign_project = foreign_project.model_copy(update={"id": str(record.id)})

    with pytest.raises(TicketReservationScopeNotFoundError, match="项目不存在"):
        _reserve(
            service=TicketReservationService(database),
            context=context,
            project_id=foreign_project.id,
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
    database.engine.dispose()


def test_wallet_lookup_uses_postgresql_row_lock() -> None:
    class RecordingSession:
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return TicketWalletRecord(
                user_id=1,
                available_microtickets=0,
                held_microtickets=0,
                lifetime_granted_microtickets=0,
                lifetime_spent_microtickets=0,
                version=1,
            )

    session = RecordingSession()
    TicketWalletService.require_wallet_for_update(session, 1)  # type: ignore[arg-type]

    compiled = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
