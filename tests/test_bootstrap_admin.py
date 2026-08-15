from __future__ import annotations

import pytest

from src.platform.bootstrap_admin import (
    BootstrapAlreadyCompletedError,
    PlatformAdminBootstrapService,
)
from sqlalchemy import func, select

from src.platform.db_models import (
    AuthSessionRecord,
    AuditEventRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UserRecord,
    WorkspaceRecord,
)
from tests.test_content_repositories import RepositoryDatabase


def _database():
    database = RepositoryDatabase()
    TicketWalletRecord.__table__.create(database.engine)
    TicketLedgerRecord.__table__.create(database.engine)
    AuthSessionRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    return database


def test_bootstrap_admin_creates_complete_user_and_is_idempotent() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database, "s" * 32)

    first = service.bootstrap("13800138000", "SecurePass123")
    second = service.bootstrap("+8613800138000", None)

    assert first.created is True
    assert first.promoted is True
    assert second.created is False
    assert second.promoted is False
    assert second.user_id == first.user_id
    with database.session_factory() as session:
        user = session.get(UserRecord, int(first.user_id))
        assert user is not None
        assert user.is_platform_admin is True
        assert session.scalar(
            select(func.count()).select_from(WorkspaceRecord).where(
                WorkspaceRecord.user_id == user.id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(TicketWalletRecord).where(
                TicketWalletRecord.user_id == user.id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AuthSessionRecord).where(
                AuthSessionRecord.user_id == user.id
            )
        ) == 1
    database.engine.dispose()


def test_bootstrap_cannot_promote_a_second_platform_admin() -> None:
    database = _database()
    service = PlatformAdminBootstrapService(database, "s" * 32)
    service.bootstrap("13800138000", "SecurePass123")

    with pytest.raises(BootstrapAlreadyCompletedError, match="不能通过初始化命令"):
        service.bootstrap("13900139000", "SecurePass123")

    database.engine.dispose()
