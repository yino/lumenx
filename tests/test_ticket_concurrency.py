from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from src.platform.db_models import (
    AITaskRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
    UserRecord,
    WorkspaceRecord,
)
from src.platform.ticket_reservation import (
    InsufficientTicketBalanceError,
    TicketReservationService,
)
from src.platform.ticket_settlement import TicketSettlementService
from src.platform.ticket_wallet import TicketWalletService
from tests.test_content_repositories import _create_scope
from tests.test_ticket_settlement import LLM_FORMULA


class SerializedSqliteDatabase:
    def __init__(self, path) -> None:
        self.engine = create_engine(
            f"sqlite+pysqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            class_=Session,
        )
        UserRecord.__table__.create(self.engine)
        WorkspaceRecord.__table__.create(self.engine)
        TicketWalletRecord.__table__.create(self.engine)
        AITaskRecord.__table__.create(self.engine)
        TicketHoldRecord.__table__.create(self.engine)
        TicketLedgerRecord.__table__.create(self.engine)
        UsageEventRecord.__table__.create(self.engine)

    @contextmanager
    def transaction(self, _identity):
        session = self.session_factory()
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _database(path, initial_balance: int = 3_000_000):
    database = SerializedSqliteDatabase(path)
    context = _create_scope(database)
    with database.transaction(context.identity) as session:
        TicketWalletService.create_wallet_in_session(
            session,
            context.identity.user_id,
            initial_balance,
            reason="创建并发测试钱包",
        )
    return database, context


def _reserve(
    database,
    context,
    *,
    key: str,
    maximum_metering_tokens: int = 1500,
    tokens_per_ticket: int = 1000,
):
    return TicketReservationService(database).reserve_task(
        context,
        capability="script.analysis",
        idempotency_key=key,
        request_payload={"content": "并发测试剧本", "parameters": {}},
        config_snapshot={
            "config_version_id": str(uuid.uuid4()),
            "capability": "script.analysis",
            "parameters": {},
            "metering_formula": LLM_FORMULA,
        },
        maximum_metering_tokens=maximum_metering_tokens,
        tokens_per_ticket=tokens_per_ticket,
    )


def test_simultaneous_reservations_never_overspend_wallet(tmp_path) -> None:
    database, context = _database(tmp_path / "reservation-race.db", 2_000_000)
    barrier = Barrier(2)

    def attempt(key: str):
        barrier.wait()
        try:
            return "reserved", _reserve(database, context, key=key)
        except InsufficientTicketBalanceError as exc:
            return "insufficient", exc

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, ["race-key-1", "race-key-2"]))

        assert [state for state, _result in outcomes].count("reserved") == 1
        assert [state for state, _result in outcomes].count("insufficient") == 1
        with database.session_factory() as session:
            wallet = session.get(
                TicketWalletRecord,
                int(context.identity.user_id),
            )
            assert wallet is not None
            assert wallet.available_microtickets == 500_000
            assert wallet.held_microtickets == 1_500_000
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
    finally:
        database.engine.dispose()


def test_concurrent_idempotent_reservation_returns_one_task_and_hold(tmp_path) -> None:
    database, context = _database(tmp_path / "idempotency-race.db")
    barrier = Barrier(2)

    def attempt():
        barrier.wait()
        return _reserve(database, context, key="same-race-key")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: attempt(), range(2)))

        assert len({result.task_id for result in results}) == 1
        assert len({result.hold_id for result in results}) == 1
        assert sorted(result.reused for result in results) == [False, True]
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 2
    finally:
        database.engine.dispose()


def test_concurrent_settlement_and_release_deliveries_are_idempotent(tmp_path) -> None:
    settlement_db, settlement_context = _database(tmp_path / "settlement-race.db")
    settlement = _reserve(settlement_db, settlement_context, key="settlement-race")
    with settlement_db.transaction(settlement_context.identity) as session:
        task = session.get(AITaskRecord, int(settlement.task_id))
        assert task is not None
        task.status = "provider_succeeded"
    settlement_barrier = Barrier(2)

    def settle():
        settlement_barrier.wait()
        return TicketSettlementService(settlement_db).settle_success(
            settlement_context,
            task_id=settlement.task_id,
            raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
        )

    release_db, release_context = _database(tmp_path / "release-race.db")
    release = _reserve(release_db, release_context, key="release-race")
    with release_db.transaction(release_context.identity) as session:
        task = session.get(AITaskRecord, int(release.task_id))
        assert task is not None
        task.status = "running"
    release_barrier = Barrier(2)

    def release_hold():
        release_barrier.wait()
        return TicketSettlementService(release_db).release_nonbillable(
            release_context,
            task_id=release.task_id,
            outcome="nonbillable_failure",
            reason="供应商确认未计费",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            settlements = list(executor.map(lambda _index: settle(), range(2)))
        with ThreadPoolExecutor(max_workers=2) as executor:
            releases = list(executor.map(lambda _index: release_hold(), range(2)))

        assert len({item.usage_event_id for item in settlements}) == 1
        assert sorted(item.reused for item in settlements) == [False, True]
        assert len({item.usage_event_id for item in releases}) == 1
        assert sorted(item.reused for item in releases) == [False, True]
        with settlement_db.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 4
        with release_db.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 3
    finally:
        settlement_db.engine.dispose()
        release_db.engine.dispose()


def test_tasks_settle_with_their_own_exchange_rate_snapshots(tmp_path) -> None:
    database, context = _database(tmp_path / "exchange-snapshot.db", 5_000_000)
    try:
        first = _reserve(
            database,
            context,
            key="exchange-v1",
            maximum_metering_tokens=2000,
            tokens_per_ticket=1000,
        )
        second = _reserve(
            database,
            context,
            key="exchange-v2",
            maximum_metering_tokens=2000,
            tokens_per_ticket=2000,
        )
        with database.transaction(context.identity) as session:
            for task_id in [first.task_id, second.task_id]:
                task = session.get(AITaskRecord, int(task_id))
                assert task is not None
                task.status = "provider_succeeded"

        first_result = TicketSettlementService(database).settle_success(
            context,
            task_id=first.task_id,
            raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
        )
        second_result = TicketSettlementService(database).settle_success(
            context,
            task_id=second.task_id,
            raw_provider_usage={"input_tokens": 100, "output_tokens": 50},
        )

        assert first_result.tokens_per_ticket == 1000
        assert first_result.charged_microtickets == 200_000
        assert second_result.tokens_per_ticket == 2000
        assert second_result.charged_microtickets == 100_000
    finally:
        database.engine.dispose()
