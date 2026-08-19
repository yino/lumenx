from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.platform.admin_access import (
    AdminConsoleUnavailableError,
    create_require_platform_admin,
    enforce_admin_rate_limit,
    require_admin_console_enabled,
    require_platform_admin_context,
)
from src.platform.auth.admin import AdminAuthorizationError
from src.platform.auth.admin_identity import AdminSessionPrincipal
from src.platform.auth.sessions import SessionAuthenticationError
from src.platform.contracts import AdminContext, UserContext


def _principal() -> AdminSessionPrincipal:
    return AdminSessionPrincipal(
        admin_id=1,
        session_id=2,
        username="admin",
        must_change_password=True,
    )


def test_independent_admin_dependency_requires_admin_cookie_and_csrf_before_handler() -> None:
    sessions = Mock()
    sessions.resolve.return_value = _principal()
    require_admin = create_require_platform_admin(sessions)
    app = FastAPI()

    @app.post("/admin/probe")
    def probe(identity: AdminContext = Depends(require_admin)) -> dict[str, str]:
        return {"admin_id": identity.admin_id}

    @app.exception_handler(AdminAuthorizationError)
    def denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(status_code=403, content={"code": "ADMIN_REQUIRED", "message": str(exc)})

    @app.exception_handler(SessionAuthenticationError)
    def unauthenticated(_request: Request, exc: SessionAuthenticationError):
        return JSONResponse(status_code=401, content={"code": exc.code, "message": str(exc)})

    client = TestClient(app)
    client.cookies.set("lumenx_admin_session", "opaque-token")
    response = client.post("/admin/probe", headers={"x-csrf-token": "csrf-proof"})

    assert response.json() == {"admin_id": "1"}
    sessions.resolve.assert_called_once_with("opaque-token", csrf_token="csrf-proof")

    sessions.resolve.reset_mock()
    missing_csrf = client.post("/admin/probe")
    assert missing_csrf.status_code == 401
    assert missing_csrf.json()["code"] == "ADMIN_CSRF_INVALID"
    sessions.resolve.assert_not_called()

    sessions.resolve.reset_mock()
    client.cookies.delete("lumenx_admin_session")
    client.cookies.set("lumenx_session", "normal-user-token")
    denied_response = client.post("/admin/probe", headers={"x-csrf-token": "csrf-proof"})
    assert denied_response.status_code == 401
    assert "admin_id" not in denied_response.text
    sessions.resolve.assert_not_called()


def test_service_guard_rejects_missing_and_normal_user_before_database_access() -> None:
    with pytest.raises(AdminAuthorizationError):
        require_platform_admin_context(None)
    with pytest.raises(AdminAuthorizationError):
        require_platform_admin_context(UserContext(user_id="7"))


def test_expanded_console_feature_flags_fail_closed_independently() -> None:
    require_admin_console_enabled(
        SimpleNamespace(admin_console_enabled=True, admin_console_mutations_enabled=True)
    )
    with pytest.raises(AdminConsoleUnavailableError, match="扩展功能已停用"):
        require_admin_console_enabled(
            SimpleNamespace(admin_console_enabled=False, admin_console_mutations_enabled=True)
        )
    with pytest.raises(AdminConsoleUnavailableError) as captured:
        require_admin_console_enabled(
            SimpleNamespace(admin_console_enabled=True, admin_console_mutations_enabled=False),
            mutation=True,
        )
    assert captured.value.code == "ADMIN_MUTATIONS_DISABLED"


@pytest.mark.parametrize(
    ("rate_class", "expected_limit"),
    [("mutation", 12), ("sensitive_read", 6), ("export", 2)],
)
def test_admin_rate_limit_uses_operation_class_and_private_identity_key(
    rate_class: str,
    expected_limit: int,
) -> None:
    limiter = Mock()
    application = SimpleNamespace(rate_limiter=limiter)
    settings = SimpleNamespace(
        deployment_mode="cloud",
        admin_rate_window_seconds=60,
        admin_mutation_limit=12,
        admin_sensitive_read_limit=6,
        admin_export_limit=2,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/probe",
            "headers": [],
            "client": ("203.0.113.7", 443),
        }
    )

    enforce_admin_rate_limit(
        application,
        settings,
        request,
        AdminContext(admin_id="23"),
        action="probe",
        rate_class=rate_class,
    )

    args, kwargs = limiter.check.call_args
    assert args[0] == "admin_probe"
    assert args[1].maximum_attempts == expected_limit
    assert args[1].window_seconds == 60
    assert kwargs == {
        "phone_canonical": "admin:23",
        "network_source": "203.0.113.7",
    }


def test_admin_rate_limit_fails_closed_in_cloud_without_limiter() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/probe",
            "headers": [],
            "client": None,
        }
    )
    settings = SimpleNamespace(
        deployment_mode="cloud",
        admin_rate_window_seconds=60,
        admin_mutation_limit=12,
        admin_sensitive_read_limit=6,
        admin_export_limit=2,
    )

    with pytest.raises(AdminConsoleUnavailableError) as captured:
        enforce_admin_rate_limit(
            SimpleNamespace(),
            settings,
            request,
            AdminContext(admin_id="23"),
            action="probe",
        )

    assert captured.value.code == "ADMIN_RATE_LIMIT_UNAVAILABLE"
