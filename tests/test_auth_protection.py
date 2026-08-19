from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.protection import (
    AuthRateLimiter,
    CookieSecurityMiddleware,
    RateLimitExceeded,
    RateLimitPolicy,
)


def build_security_app() -> TestClient:
    app = FastAPI()
    app.add_middleware(
        CookieSecurityMiddleware,
        allowed_origins=frozenset({"https://studio.example.com"}),
    )

    @app.post("/mutate")
    def mutate():
        return {"ok": True}

    @app.post("/admin/auth/login")
    @app.post("/api/v1/admin/auth/login")
    def admin_login():
        return {"ok": True}

    @app.post("/admin/mutate")
    def admin_mutate():
        return {"ok": True}

    return TestClient(app)


def test_cookie_mutation_requires_same_origin_and_csrf() -> None:
    client = build_security_app()
    client.cookies.set("lumenx_session", "session-token")
    client.cookies.set("lumenx_csrf", "csrf-token")

    denied_origin = client.post(
        "/mutate",
        headers={"origin": "https://evil.example", "x-csrf-token": "csrf-token"},
    )
    denied_csrf = client.post(
        "/mutate",
        headers={"origin": "https://studio.example.com", "x-csrf-token": "wrong"},
    )
    allowed = client.post(
        "/mutate",
        headers={"origin": "https://studio.example.com", "x-csrf-token": "csrf-token"},
    )

    assert denied_origin.status_code == 403
    assert denied_origin.json()["code"] == "ORIGIN_DENIED"
    assert denied_csrf.json()["code"] == "CSRF_INVALID"
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/admin/auth/login", "/api/v1/admin/auth/login"],
)
def test_normal_user_cookie_does_not_block_independent_admin_login(path: str) -> None:
    client = build_security_app()
    client.cookies.set("lumenx_session", "normal-user-session")
    client.cookies.set("lumenx_csrf", "normal-user-csrf")

    response = client.post(path)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_admin_cookie_uses_independent_origin_and_csrf_namespace() -> None:
    client = build_security_app()
    client.cookies.set("lumenx_session", "normal-user-session")
    client.cookies.set("lumenx_csrf", "normal-user-csrf")
    client.cookies.set("lumenx_admin_session", "admin-session")
    client.cookies.set("lumenx_admin_csrf", "admin-csrf")

    normal_csrf = client.post(
        "/admin/mutate",
        headers={
            "origin": "https://studio.example.com",
            "x-csrf-token": "normal-user-csrf",
        },
    )
    denied_origin = client.post(
        "/admin/mutate",
        headers={
            "origin": "https://evil.example",
            "x-csrf-token": "admin-csrf",
        },
    )
    allowed = client.post(
        "/admin/mutate",
        headers={
            "origin": "https://studio.example.com",
            "x-csrf-token": "admin-csrf",
        },
    )

    assert normal_csrf.status_code == 403
    assert normal_csrf.json()["code"] == "CSRF_INVALID"
    assert denied_origin.status_code == 403
    assert denied_origin.json()["code"] == "ORIGIN_DENIED"
    assert allowed.status_code == 200


def test_rate_limiter_checks_phone_and_network_without_raw_values() -> None:
    redis = Mock()
    redis.eval.side_effect = [1, 1, 4]
    redis.ttl.return_value = 120
    limiter = AuthRateLimiter(redis, "s" * 32)
    policy = RateLimitPolicy(maximum_attempts=3, window_seconds=900)

    limiter.check(
        "login",
        policy,
        phone_canonical="+8613800138000",
        network_source="203.0.113.8",
    )
    with pytest.raises(RateLimitExceeded) as exc_info:
        limiter.check(
            "login",
            policy,
            phone_canonical="+8613800138000",
            network_source="203.0.113.8",
        )

    assert exc_info.value.retry_after_seconds == 120
    evaluated_keys = [call.args[2] for call in redis.eval.call_args_list]
    assert all("13800138000" not in key and "203.0.113.8" not in key for key in evaluated_keys)
