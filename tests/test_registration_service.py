from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from src.platform.auth import DuplicatePhoneError
from src.platform.auth.registration import RegistrationPolicy, RegistrationService
from src.platform.configuration_schemas import RegistrationMode
from src.platform.db_models import (
    AuthSessionRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UserRecord,
    WorkspaceRecord,
)


class RecordingSession:
    def __init__(self, fail_on_flush_at: int | None = None) -> None:
        self.records: list[object] = []
        self.pending: list[object] = []
        self.flushed_batches: list[tuple[type[object], ...]] = []
        self.fail_on_flush_at = fail_on_flush_at
        self.next_id = 1
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def add(self, record: object) -> None:
        self.records.append(record)
        self.pending.append(record)

    def add_all(self, records: list[object]) -> None:
        self.records.extend(records)
        self.pending.extend(records)

    def flush(self) -> None:
        for record in self.pending:
            if hasattr(record, "id") and getattr(record, "id", None) is None:
                record.id = self.next_id
                self.next_id += 1
        batch = tuple(type(record) for record in self.pending)
        self.flushed_batches.append(batch)
        self.pending.clear()
        if self.fail_on_flush_at == len(self.flushed_batches):
            raise RuntimeError("forced rollback")


class RecordingDatabase:
    def __init__(self, fail_on_flush_at: int | None = None) -> None:
        self.session = RecordingSession(fail_on_flush_at=fail_on_flush_at)
        self.committed = False
        self.rolled_back = False
        self.identity = None

    @contextmanager
    def transaction(self, identity=None):
        self.identity = identity
        try:
            yield self.session
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True


def _open_policy(**changes) -> RegistrationPolicy:
    return RegistrationPolicy(
        registration_mode=RegistrationMode.OPEN,
        **changes,
    )


def test_registration_policy_defaults_to_disabled() -> None:
    database = RecordingDatabase()

    with pytest.raises(PermissionError, match="注册暂未开放"):
        RegistrationService(database, "s" * 32).register(
            "13800138000",
            "secure-pass-2026",
        )

    assert database.committed is False


def test_registration_creates_all_records_in_one_transaction() -> None:
    database = RecordingDatabase()
    service = RegistrationService(
        database,
        "s" * 32,
        policy=_open_policy(initial_grant_microtickets=2_500_000),
    )

    result = service.register("13800138000", "secure-pass-2026")

    assert database.committed is True
    assert database.rolled_back is False
    assert database.identity is None
    assert {type(record) for record in database.session.records} == {
        UserRecord,
        TicketWalletRecord,
        TicketLedgerRecord,
        WorkspaceRecord,
        AuthSessionRecord,
    }
    user = next(record for record in database.session.records if isinstance(record, UserRecord))
    wallet = next(
        record for record in database.session.records if isinstance(record, TicketWalletRecord)
    )
    ledger = next(
        record for record in database.session.records if isinstance(record, TicketLedgerRecord)
    )
    assert user.phone_verified_at is None
    assert wallet.available_microtickets == 2_500_000
    assert ledger.available_after == 2_500_000
    assert result.session.token not in result.session.record.token_hash
    assert database.session.flushed_batches == [
        (UserRecord,),
        (TicketWalletRecord,),
        (TicketLedgerRecord, WorkspaceRecord, AuthSessionRecord),
    ]


def test_registration_rolls_back_all_records_on_failure() -> None:
    database = RecordingDatabase(fail_on_flush_at=3)
    service = RegistrationService(database, "s" * 32, policy=_open_policy())

    with pytest.raises(RuntimeError, match="forced rollback"):
        service.register("13800138000", "secure-pass-2026")

    assert database.committed is False
    assert database.rolled_back is True


def test_registration_translates_database_phone_race() -> None:
    class DuplicateSession(RecordingSession):
        def flush(self) -> None:
            original = Exception("duplicate")
            original.diag = SimpleNamespace(constraint_name="uq_users_phone_canonical")
            raise IntegrityError("insert", {}, original)

    database = RecordingDatabase()
    database.session = DuplicateSession()
    service = RegistrationService(database, "s" * 32, policy=_open_policy())

    with pytest.raises(DuplicatePhoneError, match="已注册"):
        service.register("13800138000", "secure-pass-2026")

    assert database.rolled_back is True
