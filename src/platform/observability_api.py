from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Request

from .auth.admin import AdminAuthorizationError
from .auth.api import AuthApplication
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .contracts import UserContext
from .observability import MetricsRegistry, metrics


def install_observability_api(
    app: FastAPI,
    auth: AuthApplication,
    *,
    registry: MetricsRegistry = metrics,
) -> MetricsRegistry:
    router = APIRouter(prefix="/admin/observability", tags=["平台观测"])

    def require_admin(request: Request) -> UserContext:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        principal: SessionPrincipal = auth.sessions.resolve(token)
        if not principal.is_platform_admin:
            raise AdminAuthorizationError("仅平台管理员可以查看平台观测指标")
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=True,
        )

    @router.get("/metrics")
    def get_metrics(_identity: UserContext = Depends(require_admin)) -> dict:
        return registry.snapshot()

    app.include_router(router)
    app.state.metrics_registry = registry
    return registry
