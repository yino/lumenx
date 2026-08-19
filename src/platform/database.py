from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .contracts import AdminContext, SystemContext, UserContext
from .identifiers import parse_database_id


def _uses_non_postgres_database(session: Session) -> bool:
    dialect_name = getattr(getattr(session, "bind", None), "dialect", None)
    dialect_name = getattr(dialect_name, "name", None)
    return isinstance(dialect_name, str) and dialect_name != "postgresql"


def set_transaction_user_context(session: Session, identity: UserContext) -> None:
    if not identity.user_id.strip():
        raise ValueError("数据库事务必须包含有效用户标识")
    if _uses_non_postgres_database(session):
        return

    session.execute(
        text(
            """
            SELECT
                set_config('app.current_user_id', :user_id, true),
                set_config('app.current_session_id', :session_id, true)
            """
        ),
        {
            "user_id": str(
                parse_database_id(identity.user_id, field="用户 ID", allow_zero=True)
            ),
            "session_id": identity.session_id or "",
        },
    )


def set_transaction_admin_context(session: Session, identity: AdminContext) -> None:
    if not identity.admin_id.strip():
        raise ValueError("数据库事务必须包含有效管理员标识")
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text(
            """
            SELECT
                set_config('app.current_admin_id', :admin_id, true),
                set_config('app.current_admin_session_id', :session_id, true)
            """
        ),
        {
            "admin_id": str(parse_database_id(identity.admin_id, field="管理员 ID")),
            "session_id": identity.session_id or "",
        },
    )


def set_transaction_system_context(session: Session, identity: SystemContext) -> None:
    if not identity.service_name.strip():
        raise ValueError("数据库事务必须包含有效系统服务名称")
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text(
            """
            SELECT
                set_config('app.current_admin_id', '0', true),
                set_config('app.current_system_actor', :service_name, true)
            """
        ),
        {"service_name": identity.service_name.strip()},
    )


def set_transaction_login_phone(session: Session, phone_canonical: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.login_phone_canonical', :phone, true)"),
        {"phone": phone_canonical},
    )


def set_transaction_login_username(session: Session, username: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.login_username', :username, true)"),
        {"username": username},
    )


def set_transaction_admin_login_username(session: Session, username: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.admin_login_username', :username, true)"),
        {"username": username},
    )


def set_transaction_registration_phone(session: Session, phone_canonical: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.registration_phone_canonical', :phone, true)"),
        {"phone": phone_canonical},
    )


def set_transaction_session_token_hash(session: Session, token_hash: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.session_token_hash', :token_hash, true)"),
        {"token_hash": token_hash},
    )


def set_transaction_admin_session_token_hash(session: Session, token_hash: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.admin_session_token_hash', :token_hash, true)"),
        {"token_hash": token_hash},
    )


def set_transaction_reset_token_hash(session: Session, token_hash: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.reset_token_hash', :token_hash, true)"),
        {"token_hash": token_hash},
    )


def set_transaction_invitation_hash(session: Session, invitation_hash: str) -> None:
    if _uses_non_postgres_database(session):
        return
    session.execute(
        text("SELECT set_config('app.registration_invitation_hash', :invitation_hash, true)"),
        {"invitation_hash": invitation_hash},
    )


def _reset_postgres_connection(
    dbapi_connection: Any,
    _connection_record: Any,
    reset_state: Any,
) -> None:
    if reset_state.terminate_only:
        return

    dbapi_connection.rollback()
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("RESET ALL")
    finally:
        cursor.close()
        dbapi_connection.rollback()


def _install_postgres_pool_reset(engine: Engine) -> None:
    if engine.dialect.name == "postgresql":
        event.listen(engine, "reset", _reset_postgres_connection)


class Database:
    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.engine = create_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
            pool_reset_on_return=None,
        )
        _install_postgres_pool_reset(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )

    @contextmanager
    def transaction(
        self,
        identity: UserContext | AdminContext | SystemContext | None = None,
    ) -> Iterator[Session]:
        session = self.session_factory()
        try:
            with session.begin():
                if isinstance(identity, UserContext):
                    set_transaction_user_context(session, identity)
                elif isinstance(identity, AdminContext):
                    set_transaction_admin_context(session, identity)
                elif isinstance(identity, SystemContext):
                    set_transaction_system_context(session, identity)
                yield session
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
