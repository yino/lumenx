from __future__ import annotations

from datetime import UTC, datetime, timedelta
import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from .admin_access import (
    create_require_platform_admin,
    enforce_admin_rate_limit,
    require_admin_console_enabled,
)
from .admin_inspection import (
    AdminInspectionNotFoundError,
    AdminInspectionService,
    AdminInspectionValidationError,
    ExportResource,
    InspectionResource,
)
from .auth.api import AuthApplication
from .contracts import AdminContext
from .error_protocol import request_correlation_id
from .media_storage import CloudMediaStorage, MediaStorageError
from .observability import metrics
from .settings import DeploymentSettings


class SensitiveAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: PositiveInt
    purpose: str = Field(min_length=1, max_length=2000)


def install_cloud_admin_inspection_api(
    app: FastAPI,
    auth: AuthApplication,
    settings: DeploymentSettings,
    media_storage: CloudMediaStorage,
) -> AdminInspectionService:
    service = AdminInspectionService(auth.database, media_storage)
    router = APIRouter(prefix="/admin", tags=["管理员资源审查"])
    require_admin = create_require_platform_admin(
        auth.admin_sessions,
        denied_message="仅平台管理员可以审查平台资源",
    )

    def require_console(identity: AdminContext = Depends(require_admin)) -> AdminContext:
        require_admin_console_enabled(settings)
        return identity

    @router.get("/dashboard")
    def dashboard(
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        try:
            effective_end = end_at or datetime.now(UTC)
            effective_start = start_at or effective_end - timedelta(days=1)
            result = service.dashboard(identity, start_at=effective_start, end_at=effective_end)
            metrics.increment("admin_dashboard_queries_total", labels={"outcome": "succeeded"})
            return result
        finally:
            metrics.observe(
                "admin_dashboard_query_duration_seconds",
                max(time.perf_counter() - started_at, 0),
            )

    @router.get("/exceptions")
    def exceptions(
        limit: int = Query(default=50, ge=1, le=200),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return service.exceptions(identity, limit=limit)

    @router.get("/users/{user_id}/overview")
    def user_overview(
        user_id: PositiveInt,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return service.user_overview(identity, user_id)

    @router.get("/users/{user_id}/resources/{resource}")
    def list_user_resources(
        user_id: PositiveInt,
        resource: InspectionResource,
        workspace_id: int | None = Query(default=None, ge=1),
        status: str | None = Query(default=None, max_length=80),
        asset_type: str | None = Query(default=None, max_length=32),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return service.list_resources(
            identity,
            user_id=user_id,
            workspace_id=workspace_id,
            resource=resource,
            status=status,
            asset_type=asset_type,
            offset=offset,
            limit=limit,
        )

    @router.get("/users/{user_id}/tasks/{task_id}")
    def task_detail(
        user_id: PositiveInt,
        task_id: PositiveInt,
        workspace_id: int = Query(ge=1),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return service.task_detail(
            identity,
            user_id=user_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )

    @router.post("/users/{user_id}/scripts/{project_id}/view")
    def view_script(
        user_id: PositiveInt,
        project_id: PositiveInt,
        payload: SensitiveAccessRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        enforce_admin_rate_limit(
            auth,
            settings,
            request,
            identity,
            action="script_view",
            rate_class="sensitive_read",
        )
        result = service.sensitive_script(
            identity,
            user_id=user_id,
            workspace_id=payload.workspace_id,
            project_id=project_id,
            purpose=payload.purpose,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_sensitive_reads_total", labels={"resource": "script"})
        return result

    @router.post("/users/{user_id}/tasks/{task_id}/prompt")
    def view_task_prompt(
        user_id: PositiveInt,
        task_id: PositiveInt,
        payload: SensitiveAccessRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        enforce_admin_rate_limit(
            auth,
            settings,
            request,
            identity,
            action="task_prompt_view",
            rate_class="sensitive_read",
        )
        result = service.sensitive_task_prompt(
            identity,
            user_id=user_id,
            workspace_id=payload.workspace_id,
            task_id=task_id,
            purpose=payload.purpose,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_sensitive_reads_total", labels={"resource": "task_prompt"})
        return result

    @router.post("/users/{user_id}/media/{media_id}/preview")
    def preview_media(
        user_id: PositiveInt,
        media_id: PositiveInt,
        payload: SensitiveAccessRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        enforce_admin_rate_limit(
            auth,
            settings,
            request,
            identity,
            action="media_preview",
            rate_class="sensitive_read",
        )
        result = service.media_preview(
            identity,
            user_id=user_id,
            workspace_id=payload.workspace_id,
            media_id=media_id,
            purpose=payload.purpose,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_sensitive_reads_total", labels={"resource": "media"})
        return result

    @router.post("/exports/{resource}.csv")
    def export_csv(
        resource: ExportResource,
        request: Request,
        start_at: datetime = Query(),
        end_at: datetime = Query(),
        purpose: str = Query(min_length=1, max_length=2000),
        user_id: int | None = Query(default=None, ge=1),
        status: str | None = Query(default=None, max_length=80),
        identity: AdminContext = Depends(require_console),
    ) -> Response:
        enforce_admin_rate_limit(
            auth,
            settings,
            request,
            identity,
            action=f"{resource}_export",
            rate_class="export",
        )
        content = service.export_csv(
            identity,
            resource=resource,
            start_at=start_at,
            end_at=end_at,
            user_id=user_id,
            status=status,
            purpose=purpose,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_exports_total", labels={"resource": resource})
        return Response(
            content=content.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="lumenx-{resource}.csv"'},
        )

    app.include_router(router)
    app.state.admin_inspection_service = service

    @app.exception_handler(AdminInspectionNotFoundError)
    def handle_inspection_not_found(request: Request, exc: AdminInspectionNotFoundError):
        return JSONResponse(
            status_code=404,
            content={
                "code": "ADMIN_TARGET_NOT_FOUND",
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    @app.exception_handler(AdminInspectionValidationError)
    def handle_inspection_validation(request: Request, exc: AdminInspectionValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": exc.code,
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    @app.exception_handler(MediaStorageError)
    def handle_inspection_media_error(request: Request, exc: MediaStorageError):
        return JSONResponse(
            status_code=503,
            content={
                "code": "ADMIN_MEDIA_PREVIEW_UNAVAILABLE",
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    return service
