from __future__ import annotations

import pytest
from sqlalchemy import func, select
from starlette.responses import Response

from src.platform.auth.admin_identity import (
    ADMIN_CSRF_COOKIE_NAME,
    ADMIN_SESSION_COOKIE_NAME,
    AdminAuthenticationService,
    AdminCredentialError,
    AdminSessionService,
    clear_admin_session_cookies,
    set_admin_session_cookies,
)
from src.platform.auth.security import PasswordService
from src.platform.auth.sessions import SessionAuthenticationError
from src.platform.db_models import AdminSessionRecord, AdminUserRecord, UserRecord
from tests.test_content_repositories import RepositoryDatabase


def _database() -> RepositoryDatabase:
    database = RepositoryDatabase()
    AdminUserRecord.__table__.create(database.engine)
    AdminSessionRecord.__table__.create(database.engine)
    return database


def _seed_admin(database: RepositoryDatabase, password: str = "SecureAdminPass123") -> int:
    with database.session_factory.begin() as session:
        admin = AdminUserRecord(
            username="admin",
            password_hash=PasswordService().hash(password),
            status="active",
        )
        session.add(admin)
        session.flush()
        return admin.id


def test_admin_login_session_csrf_and_logout_are_independent() -> None:
    database = _database()
    admin_id = _seed_admin(database)
    secret = "s" * 32
    authentication = AdminAuthenticationService(database, secret)
    sessions = AdminSessionService(database, secret)

    result = authentication.login("ADMIN", "SecureAdminPass123")
    principal = sessions.resolve(
        result.session.token,
        csrf_token=result.session.csrf_token,
    )

    assert principal.admin_id == admin_id
    assert principal.username == "admin"
    with pytest.raises(SessionAuthenticationError) as invalid_csrf:
        sessions.resolve(result.session.token, csrf_token="wrong-csrf")
    assert invalid_csrf.value.code == "ADMIN_CSRF_INVALID"

    sessions.logout(result.session.token)
    with pytest.raises(SessionAuthenticationError) as logged_out:
        sessions.resolve(result.session.token)
    assert logged_out.value.code == "ADMIN_AUTH_REQUIRED"
    database.engine.dispose()


def test_admin_login_rejects_normal_user_credentials() -> None:
    database = _database()
    with database.session_factory.begin() as session:
        session.add(
            UserRecord(
                username="creator",
                password_hash=PasswordService().hash("SecureUserPass123"),
                status="active",
            )
        )

    with pytest.raises(AdminCredentialError, match="管理员账号或密码不正确"):
        AdminAuthenticationService(database, "s" * 32).login(
            "creator",
            "SecureUserPass123",
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count(AdminSessionRecord.id))) == 0
    database.engine.dispose()


def test_admin_password_change_revokes_only_other_admin_sessions() -> None:
    database = _database()
    admin_id = _seed_admin(database)
    authentication = AdminAuthenticationService(database, "s" * 32)
    sessions = AdminSessionService(database, "s" * 32)
    current = authentication.login("admin", "SecureAdminPass123")
    other = authentication.login("admin", "SecureAdminPass123")
    principal = sessions.resolve(current.session.token)

    authentication.change_password(
        principal,
        "SecureAdminPass123",
        "RotatedAdminPass123",
    )

    assert sessions.resolve(current.session.token).admin_id == admin_id
    with pytest.raises(SessionAuthenticationError):
        sessions.resolve(other.session.token)
    assert authentication.login("admin", "RotatedAdminPass123").admin_id == admin_id
    with database.session_factory() as session:
        assert session.scalar(select(func.count(UserRecord.id))) == 0
    database.engine.dispose()


def test_admin_cookie_helpers_use_dedicated_names() -> None:
    database = _database()
    _seed_admin(database)
    issued = AdminAuthenticationService(database, "s" * 32).login(
        "admin",
        "SecureAdminPass123",
    ).session
    response = Response()

    set_admin_session_cookies(response, issued)

    cookies = response.headers.getlist("set-cookie")
    assert any(cookie.startswith(f"{ADMIN_SESSION_COOKIE_NAME}=") for cookie in cookies)
    assert any(cookie.startswith(f"{ADMIN_CSRF_COOKIE_NAME}=") for cookie in cookies)
    assert all(not cookie.startswith("lumenx_session=") for cookie in cookies)

    clear_response = Response()
    clear_admin_session_cookies(clear_response)
    cleared = clear_response.headers.getlist("set-cookie")
    assert any(cookie.startswith(f"{ADMIN_SESSION_COOKIE_NAME}=") for cookie in cleared)
    assert any(cookie.startswith(f"{ADMIN_CSRF_COOKIE_NAME}=") for cookie in cleared)
    database.engine.dispose()
