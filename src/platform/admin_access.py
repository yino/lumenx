from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from fastapi import Request

from .auth.admin import AdminAuthorizationError
from .auth.protection import RateLimitPolicy, UNSAFE_METHODS
from .auth.admin_identity import (
    ADMIN_SESSION_COOKIE_NAME,
    AdminSessionPrincipal,
)
from .auth.sessions import SessionAuthenticationError
from .contracts import AdminContext


class SessionResolver(Protocol):
    def resolve(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
    ) -> AdminSessionPrincipal: ...


class AdminConsoleSettings(Protocol):
    admin_console_enabled: bool
    admin_console_mutations_enabled: bool
    admin_rate_window_seconds: int
    admin_mutation_limit: int
    admin_sensitive_read_limit: int
    admin_export_limit: int


class AdminConsoleUnavailableError(RuntimeError):
    def __init__(self, message: str, *, code: str = "ADMIN_CONSOLE_DISABLED") -> None:
        super().__init__(message)
        self.code = code


def require_platform_admin_context(identity: AdminContext | None) -> AdminContext:
    if identity is None or not isinstance(identity, AdminContext):
        raise AdminAuthorizationError("仅平台管理员可以访问系统管理后台")
    if not identity.admin_id.strip():
        raise AdminAuthorizationError("管理员身份无效")
    return identity


def platform_admin_context(principal: AdminSessionPrincipal) -> AdminContext:
    return require_platform_admin_context(
        AdminContext(
            admin_id=str(principal.admin_id),
            session_id=str(principal.session_id),
            username=principal.username,
        )
    )


AdminRateClass = Literal["mutation", "sensitive_read", "export"]


def enforce_admin_rate_limit(
    application: Any,
    settings: AdminConsoleSettings,
    request: Request,
    identity: AdminContext,
    *,
    action: str,
    rate_class: AdminRateClass = "mutation",
) -> None:
    """Apply a privacy-preserving per-admin and per-network operation limit."""

    require_platform_admin_context(identity)
    limiter = getattr(application, "rate_limiter", None)
    if limiter is None:
        # Lightweight unit-test applications do not always install Redis. A real
        # cloud AuthApplication always has the shared limiter.
        deployment_mode = getattr(settings, "deployment_mode", None)
        if getattr(deployment_mode, "value", deployment_mode) in {"cloud", "test"}:
            raise AdminConsoleUnavailableError(
                "管理员操作限流服务尚未就绪",
                code="ADMIN_RATE_LIMIT_UNAVAILABLE",
            )
        return
    maximum = {
        "mutation": settings.admin_mutation_limit,
        "sensitive_read": settings.admin_sensitive_read_limit,
        "export": settings.admin_export_limit,
    }[rate_class]
    network_source = request.client.host if request.client else "unknown"
    limiter.check(
        f"admin_{action}",
        RateLimitPolicy(
            maximum_attempts=maximum,
            window_seconds=settings.admin_rate_window_seconds,
        ),
        phone_canonical=f"admin:{identity.admin_id}",
        network_source=network_source,
    )


def create_require_platform_admin_principal(
    sessions: SessionResolver,
    *,
    denied_message: str = "仅平台管理员可以访问系统管理后台",
) -> Callable[[Request], AdminSessionPrincipal]:
    def require_platform_admin(request: Request) -> AdminSessionPrincipal:
        token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME)
        if not token:
            raise SessionAuthenticationError("ADMIN_AUTH_REQUIRED", "请先登录系统管理后台")
        if request.method not in UNSAFE_METHODS:
            return sessions.resolve(token)
        csrf_token = request.headers.get("x-csrf-token")
        if not csrf_token:
            raise SessionAuthenticationError(
                "ADMIN_CSRF_INVALID",
                "管理员安全校验失败，请刷新后重试",
            )
        return sessions.resolve(token, csrf_token=csrf_token)

    return require_platform_admin


def require_admin_console_enabled(
    settings: AdminConsoleSettings,
    *,
    mutation: bool = False,
) -> None:
    if not settings.admin_console_enabled:
        raise AdminConsoleUnavailableError("系统管理后台扩展功能已停用")
    if mutation and not settings.admin_console_mutations_enabled:
        raise AdminConsoleUnavailableError(
            "系统管理后台写入操作已停用",
            code="ADMIN_MUTATIONS_DISABLED",
        )


def create_require_platform_admin(
    sessions: SessionResolver,
    *,
    denied_message: str = "仅平台管理员可以访问系统管理后台",
) -> Callable[[Request], AdminContext]:
    require_principal = create_require_platform_admin_principal(
        sessions,
        denied_message=denied_message,
    )

    def require_platform_admin(request: Request) -> AdminContext:
        return platform_admin_context(require_principal(request))

    return require_platform_admin
