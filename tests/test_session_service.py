from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from starlette.responses import Response

from src.platform.auth.sessions import (
    SessionAuthenticationError,
    SessionPolicy,
    SessionService,
    clear_session_cookies,
    hash_session_secret,
    issue_session,
    set_session_cookies,
)
from src.platform.db_models import UserRecord


class SessionDatabase:
    def __init__(self, session: Mock) -> None:
        self.session = session

    @contextmanager
    def transaction(self, _identity=None):
        yield self.session


def test_issued_session_uses_only_hashed_secrets() -> None:
    issued = issue_session(1, "s" * 32)

    assert issued.token not in issued.record.token_hash
    assert issued.csrf_token not in issued.record.csrf_token_hash
    assert issued.record.token_hash == hash_session_secret(issued.token, "s" * 32)


def test_session_cookie_security_attributes() -> None:
    policy = SessionPolicy()
    response = Response()
    issued = issue_session(1, "s" * 32, policy=policy)

    set_session_cookies(response, issued, policy)
    headers = response.headers.getlist("set-cookie")

    assert any("lumenx_session=" in value and "HttpOnly" in value for value in headers)
    assert all("Secure" in value and "SameSite=lax" in value for value in headers)
    assert any("lumenx_csrf=" in value and "HttpOnly" not in value for value in headers)

    clear_session_cookies(response)
    assert len(response.headers.getlist("set-cookie")) == 4


def test_resolve_refreshes_idle_expiry_and_returns_principal() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    user_id = 1
    issued = issue_session(user_id, "s" * 32, now=now)
    issued.record.id = 11
    user = UserRecord(
        id=user_id,
        phone_canonical="+8613800138000",
        password_hash="hash",
        status="active",
        is_platform_admin=False,
    )
    session = Mock()
    session.scalar.return_value = issued.record
    session.get.return_value = user
    service = SessionService(SessionDatabase(session), "s" * 32)

    principal = service.resolve(issued.token, now=now + timedelta(hours=1))

    assert principal.user_id == user_id
    assert principal.session_id == issued.record.id
    assert issued.record.last_seen_at == now + timedelta(hours=1)
    assert session.execute.call_count == 2


def test_expired_session_is_revoked_before_error() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    issued = issue_session(1, "s" * 32, now=now - timedelta(days=40))
    session = Mock()
    session.scalar.return_value = issued.record
    session.get.return_value = Mock(status="active")
    service = SessionService(SessionDatabase(session), "s" * 32)

    with pytest.raises(SessionAuthenticationError) as exc_info:
        service.resolve(issued.token, now=now)

    assert exc_info.value.code == "SESSION_EXPIRED"
    assert issued.record.revoked_at == now


def test_suspended_account_is_revoked_immediately() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    issued = issue_session(1, "s" * 32, now=now)
    session = Mock()
    session.scalar.return_value = issued.record
    session.get.return_value = Mock(status="suspended")
    service = SessionService(SessionDatabase(session), "s" * 32)

    with pytest.raises(SessionAuthenticationError) as exc_info:
        service.resolve(issued.token, now=now)

    assert exc_info.value.code == "ACCOUNT_SUSPENDED"
    assert issued.record.revoked_at == now


def test_persisted_csrf_hash_is_checked_during_resolution() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    user_id = 1
    issued = issue_session(user_id, "s" * 32, now=now)
    session = Mock()
    session.scalar.return_value = issued.record
    session.get.return_value = UserRecord(
        id=user_id,
        phone_canonical="+8613800138000",
        password_hash="hash",
        status="active",
        is_platform_admin=False,
    )
    service = SessionService(SessionDatabase(session), "s" * 32)

    with pytest.raises(SessionAuthenticationError) as exc_info:
        service.resolve(issued.token, csrf_token="forged-csrf", now=now)

    assert exc_info.value.code == "CSRF_INVALID"


def test_logout_revokes_matching_session() -> None:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    issued = issue_session(1, "s" * 32, now=now)
    session = Mock()
    session.scalar.return_value = issued.record
    service = SessionService(SessionDatabase(session), "s" * 32)

    service.logout(issued.token, now=now)

    assert issued.record.revoked_at == now
