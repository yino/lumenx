from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import func, select

from src.platform.db_models import TicketLedgerRecord, TicketWalletRecord
from src.platform.ticket_math import MAX_BIGINT, TicketArithmeticError
from src.platform.ticket_wallet import TicketWalletConflictError, TicketWalletService
from tests.test_content_repositories import RepositoryDatabase, _create_scope


def _wallet_database():
    database = RepositoryDatabase()
    TicketWalletRecord.__table__.create(database.engine)
    TicketLedgerRecord.__table__.create(database.engine)
    context = _create_scope(database)
    return database, context


def test_wallet_cache_tracks_append_only_ledger_mutations() -> None:
    database, context = _wallet_database()
    service = TicketWalletService(database)
    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            2_000_000,
            reason="创建测试钱包",
            correlation={"source": "test"},
        )

    with database.transaction(context.identity) as session:
        wallet = service.require_wallet_for_update(session, context.identity.user_id)
        service.hold_in_session(session, wallet, 1_000_000, reason="任务额度预占")
        service.settle_in_session(
            session,
            wallet,
            600_000,
            reason="任务实际结算",
            workspace_id=context.workspace_id,
            project_id=None,
            task_id=None,
            hold_id=None,
        )
        service.release_in_session(
            session,
            wallet,
            400_000,
            reason="释放未使用额度",
            workspace_id=context.workspace_id,
            project_id=None,
            task_id=None,
            hold_id=None,
        )
        service.credit_in_session(
            session,
            wallet,
            100_000,
            entry_type="compensation",
            reason="人工补偿",
        )
        service.debit_adjustment_in_session(
            session,
            wallet,
            200_000,
            reason="人工扣减",
            actor_user_id=context.identity.user_id,
        )

    snapshot = service.get_wallet(context.identity)
    assert snapshot.available_microtickets == 1_300_000
    assert snapshot.held_microtickets == 0
    assert snapshot.lifetime_granted_microtickets == 2_100_000
    assert snapshot.lifetime_spent_microtickets == 600_000
    assert snapshot.total_microtickets == 1_300_000
    assert snapshot.version == 6
    with database.session_factory() as session:
        entries = list(session.scalars(select(TicketLedgerRecord)))
        assert Counter(entry.entry_type for entry in entries) == Counter(
            {
                "grant": 1,
                "hold": 1,
                "settlement": 1,
                "release": 1,
                "compensation": 1,
                "adjustment": 1,
            }
        )
        adjustment = next(entry for entry in entries if entry.entry_type == "adjustment")
        assert adjustment.available_after == snapshot.available_microtickets
        assert adjustment.held_after == snapshot.held_microtickets
    database.engine.dispose()


def test_failed_wallet_mutation_leaves_cache_and_ledger_unchanged() -> None:
    database, context = _wallet_database()
    service = TicketWalletService(database)
    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            100,
            reason="创建测试钱包",
        )

    try:
        with database.transaction(context.identity) as session:
            wallet = service.require_wallet_for_update(session, context.identity.user_id)
            service.hold_in_session(session, wallet, 101, reason="超额预占")
    except TicketArithmeticError:
        pass
    else:
        raise AssertionError("超额预占必须失败")

    snapshot = service.get_wallet(context.identity)
    assert snapshot.available_microtickets == 100
    assert snapshot.held_microtickets == 0
    assert snapshot.version == 1
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
    database.engine.dispose()


def test_zero_initial_grant_creates_exact_wallet_and_ledger_state() -> None:
    database, context = _wallet_database()
    with database.transaction(context.identity) as session:
        snapshot = TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            0,
            reason="创建零初始赠送钱包",
        )

    assert snapshot.available_microtickets == 0
    assert snapshot.held_microtickets == 0
    assert snapshot.total_microtickets == 0
    assert snapshot.version == 1
    with database.session_factory() as session:
        ledger = session.scalar(select(TicketLedgerRecord))
        assert ledger is not None
        assert ledger.entry_type == "grant"
        assert ledger.amount_microtickets == 0
        assert ledger.available_after == 0
        assert ledger.held_after == 0
    database.engine.dispose()


def test_validation_failure_can_be_caught_without_dirtying_wallet() -> None:
    database, context = _wallet_database()
    service = TicketWalletService(database)
    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            500,
            reason="创建测试钱包",
        )

    with database.transaction(context.identity) as session:
        wallet = service.require_wallet_for_update(session, context.identity.user_id)
        with pytest.raises(TicketWalletConflictError, match="api_key"):
            service.hold_in_session(
                session,
                wallet,
                100,
                reason="敏感关联字段测试",
                correlation={"api_key": "sk-not-allowed-123456"},
            )
        with pytest.raises(TicketWalletConflictError, match="必须填写原因"):
            service.hold_in_session(session, wallet, 100, reason="  ")
        assert wallet.available_microtickets == 500
        assert wallet.held_microtickets == 0
        assert wallet.version == 1

    snapshot = service.get_wallet(context.identity)
    assert snapshot.available_microtickets == 500
    assert snapshot.held_microtickets == 0
    assert snapshot.version == 1
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
    database.engine.dispose()


def test_lifetime_overflow_is_rejected_before_wallet_or_ledger_mutation() -> None:
    database, context = _wallet_database()
    service = TicketWalletService(database)
    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            MAX_BIGINT,
            reason="创建边界钱包",
        )

    with database.transaction(context.identity) as session:
        wallet = service.require_wallet_for_update(session, context.identity.user_id)
        service.debit_adjustment_in_session(
            session,
            wallet,
            MAX_BIGINT,
            reason="清空可用余额",
            actor_user_id=context.identity.user_id,
        )

    with database.transaction(context.identity) as session:
        wallet = service.require_wallet_for_update(session, context.identity.user_id)
        with pytest.raises(TicketArithmeticError, match="超出可存储范围"):
            service.credit_in_session(
                session,
                wallet,
                1,
                entry_type="compensation",
                reason="触发累计赠送溢出",
            )
        assert wallet.available_microtickets == 0
        assert wallet.lifetime_granted_microtickets == MAX_BIGINT
        assert wallet.version == 2

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 2
    database.engine.dispose()
