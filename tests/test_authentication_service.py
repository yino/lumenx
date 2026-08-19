from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import uuid
from unittest.mock import Mock

import pytest

from src.platform.auth.security import PasswordService
from sqlalchemy import select

from src.platform.auth.service import (
    AuthenticationPolicy,
    AuthenticationService,
    CredentialError,
)
from src.platform.auth.sessions import SessionPolicy, issue_session
from src.platform.db_models import (
    AdminUserRecord,
    AuditEventRecord,
    AuthSessionRecord,
    Base,
    UserRecord,
    WorkspaceRecord,
)
from tests.test_content_repositories import RepositoryDatabase


class AuthenticationDatabase:
    def __init__(self, session: Mock) -> None:
        self.session = session

    @contextmanager
    def transaction(self, _identity=None):
        yield self.session


def test_unknown_phone_and_wrong_password_use_same_error() -> None:
    passwords = PasswordService()
    unknown_session = Mock()
    unknown_session.scalar.return_value = None
    known_session = Mock()
    known_session.scalar.return_value = UserRecord(
        id=1,
        phone_canonical="+8613800138000",
        password_hash=passwords.hash("correct-pass-2026"),
        status="active",
    )
    unknown_service = AuthenticationService(
        AuthenticationDatabase(unknown_session), "s" * 32, password_service=passwords
    )
    known_service = AuthenticationService(
        AuthenticationDatabase(known_session), "s" * 32, password_service=passwords
    )

    with pytest.raises(CredentialError) as unknown_error:
        unknown_service.login("13900139000", "wrong-pass-2026")
    with pytest.raises(CredentialError) as wrong_error:
        known_service.login("13800138000", "wrong-pass-2026")

    assert str(unknown_error.value) == str(wrong_error.value) == "账号或密码不正确"


def test_successful_login_creates_session_and_returns_workspace() -> None:
    passwords = PasswordService()
    user = UserRecord(
        id=1,
        phone_canonical="+8613800138000",
        password_hash=passwords.hash("correct-pass-2026"),
        status="active",
    )
    workspace_id = 1
    session = Mock()
    session.scalar.side_effect = [user, user, workspace_id]
    service = AuthenticationService(
        AuthenticationDatabase(session), "s" * 32, password_service=passwords
    )

    result = service.login("13800138000", "correct-pass-2026")

    assert result.user_id == user.id
    assert result.workspace_id == workspace_id
    assert result.phone_verified is False
    first_user_query = session.scalar.call_args_list[0].args[0]
    locked_user_query = session.scalar.call_args_list[1].args[0]
    assert first_user_query._for_update_arg is None
    assert locked_user_query._for_update_arg is not None
    session.add.assert_called_once_with(result.session.record)
    session.flush.assert_called_once_with()


class AuthenticationIntegrationDatabase(RepositoryDatabase):
    @contextmanager
    def transaction(self, identity=None):
        if identity is not None:
            self.identities.append(identity)
        with self.session_factory.begin() as session:
            yield session


def test_login_snapshots_policy_and_revokes_oldest_session_at_limit() -> None:
    database = AuthenticationIntegrationDatabase()
    Base.metadata.create_all(database.engine, checkfirst=True)
    passwords = PasswordService()
    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        user = UserRecord(
                phone_canonical="+8613800138000",
                password_hash=passwords.hash("correct-pass-2026"),
                status="active",
            )
        session.add(user)
        session.flush()
        session.add(WorkspaceRecord(user_id=user.id, name="默认工作区"))
        old_session = issue_session(
            user.id,
            "s" * 32,
            now=now - timedelta(minutes=2),
        ).record
        session.add(old_session)

    service = AuthenticationService(
        database,
        "s" * 32,
        password_service=passwords,
        policy_resolver=lambda: AuthenticationPolicy(
            session_policy=SessionPolicy(idle_seconds=120, absolute_seconds=600),
            max_sessions_per_user=1,
        ),
    )
    result = service.login("13800138000", "correct-pass-2026")

    with database.session_factory() as session:
        persisted_old = session.get(AuthSessionRecord, old_session.id)
        persisted_new = session.get(AuthSessionRecord, result.session.record.id)
        event = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.action == "auth.session.limit_revoke"
            )
        )
        assert persisted_old.revoked_at is not None
        assert persisted_new.idle_timeout_seconds == 120
        assert (
            persisted_new.absolute_expires_at - persisted_new.created_at
        ).total_seconds() == 600
        assert event.target_id == str(old_session.id)
    database.engine.dispose()


def test_normal_user_login_does_not_accept_independent_admin_credentials() -> None:
    database = AuthenticationIntegrationDatabase()
    Base.metadata.create_all(database.engine, checkfirst=True)
    passwords = PasswordService()
    with database.session_factory.begin() as session:
        admin = AdminUserRecord(
            username="admin",
            password_hash=passwords.hash("sk532359025"),
            status="active",
        )
        session.add(admin)

    with pytest.raises(CredentialError, match="账号或密码不正确"):
        AuthenticationService(
            database,
            "s" * 32,
            password_service=passwords,
        ).login("ADMIN", "sk532359025")
    database.engine.dispose()
