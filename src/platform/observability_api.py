from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Request

from .admin_access import create_require_platform_admin
from .auth.api import AuthApplication
from .contracts import AdminContext
from .observability import MetricsRegistry, metrics


def install_observability_api(
    app: FastAPI,
    auth: AuthApplication,
    *,
    registry: MetricsRegistry = metrics,
) -> MetricsRegistry:
    router = APIRouter(prefix="/admin/observability", tags=["平台观测"])

    require_admin = create_require_platform_admin(
        auth.admin_sessions,
        denied_message="仅平台管理员可以查看平台观测指标",
    )

    @router.get("/metrics")
    def get_metrics(_identity: AdminContext = Depends(require_admin)) -> dict:
        return registry.snapshot()

    app.include_router(router)
    app.state.metrics_registry = registry
    return registry
