#!/usr/bin/env python3
from __future__ import annotations

from contextlib import contextmanager
import json
import time
from typing import Iterator, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.platform.auth.registration import RegistrationPolicy, RegistrationService
from src.platform.configuration_schemas import RegistrationMode
from src.platform.contracts import UserContext
from src.platform.database import Database, set_transaction_user_context
from src.platform.db_models import (
    AuthSessionRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UserRecord,
    WorkspaceRecord,
)
from src.platform.settings import DeploymentMode, get_deployment_settings


class _RollbackDatabase:
    def __init__(self, session: Session) -> None:
        self.session = session

    @contextmanager
    def transaction(
        self,
        identity: UserContext | None = None,
    ) -> Iterator[Session]:
        if identity is not None:
            set_transaction_user_context(self.session, identity)
        yield self.session


def _count(session: Session, record_type, user_id: int) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(record_type)
            .where(record_type.user_id == user_id)
        )
        or 0
    )


def main() -> int:
    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.CLOUD or not settings.database_url:
        raise RuntimeError("注册事务验证仅支持已配置 PostgreSQL 的云端模式")

    database = Database(settings.database_url)
    connection = database.engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    phone = f"199{time.time_ns() % 100_000_000:08d}"
    try:
        rollback_database = cast(Database, _RollbackDatabase(session))
        result = RegistrationService(
            rollback_database,
            settings.session_secret.get_secret_value(),
            policy=RegistrationPolicy(registration_mode=RegistrationMode.OPEN),
        ).register(phone, "RegistrationSmoke!2026")
        user_count = int(
            session.scalar(
                select(func.count())
                .select_from(UserRecord)
                .where(UserRecord.id == result.user_id)
            )
            or 0
        )
        counts = {
            "users": user_count,
            "ticket_wallets": _count(session, TicketWalletRecord, result.user_id),
            "ticket_ledger": _count(session, TicketLedgerRecord, result.user_id),
            "workspaces": _count(session, WorkspaceRecord, result.user_id),
            "auth_sessions": _count(session, AuthSessionRecord, result.user_id),
        }
        if any(count != 1 for count in counts.values()):
            raise RuntimeError(f"注册事务记录不完整: {counts}")
        print(
            json.dumps(
                {
                    "status": "passed",
                    "records": counts,
                    "phone_verified": result.phone_verified,
                    "transaction_rolled_back": True,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        session.close()
        if outer_transaction.is_active:
            outer_transaction.rollback()
        connection.close()
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
