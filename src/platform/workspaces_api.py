from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .admin_access import create_require_platform_admin
from .auth.api import AuthApplication, MessageResponse
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .contracts import AdminContext, UserContext
from .workspaces import WorkspaceConflictError, WorkspaceNotFoundError, WorkspaceService


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceRenameRequest(WorkspaceCreateRequest):
    expected_version: int = Field(gt=0)


class WorkspaceResponse(BaseModel):
    id: int
    name: str
    version: int
    deleted: bool
    retention_expires_at: str | None


class WorkspaceCleanupResponse(BaseModel):
    deleted: int
    skipped: int


def _response(record) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=record.id,
        name=record.name,
        version=record.version,
        deleted=record.deleted_at is not None,
        retention_expires_at=(
            record.retention_expires_at.isoformat() if record.retention_expires_at else None
        ),
    )


def install_workspace_api(app: FastAPI, auth: AuthApplication) -> WorkspaceService:
    service = WorkspaceService(auth.database)
    router = APIRouter(tags=["工作区"])

    def require_principal(request: Request) -> SessionPrincipal:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf_token = (
            request.headers.get("x-csrf-token") if request.method in UNSAFE_METHODS else None
        )
        return auth.sessions.resolve(token, csrf_token=csrf_token)

    def identity(principal: SessionPrincipal) -> UserContext:
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
        )

    require_admin = create_require_platform_admin(
        auth.admin_sessions,
        denied_message="仅平台管理员可以执行工作区清理",
    )

    @router.post("/workspaces", response_model=WorkspaceResponse, status_code=201)
    def create_workspace(
        payload: WorkspaceCreateRequest,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> WorkspaceResponse:
        return _response(service.create(identity(principal), payload.name))

    @router.get("/workspaces", response_model=list[WorkspaceResponse])
    def list_workspaces(
        include_deleted: bool = Query(default=False),
        principal: SessionPrincipal = Depends(require_principal),
    ) -> list[WorkspaceResponse]:
        return [
            _response(record)
            for record in service.list(identity(principal), include_deleted=include_deleted)
        ]

    @router.patch("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
    def rename_workspace(
        workspace_id: int,
        payload: WorkspaceRenameRequest,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> WorkspaceResponse:
        return _response(
            service.rename(
                identity(principal),
                workspace_id,
                payload.name,
                payload.expected_version,
            )
        )

    @router.post("/workspaces/{workspace_id}/select", response_model=WorkspaceResponse)
    def select_workspace(
        workspace_id: int,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> WorkspaceResponse:
        return _response(service.select(identity(principal), workspace_id))

    @router.delete("/workspaces/{workspace_id}", response_model=MessageResponse)
    def delete_workspace(
        workspace_id: int,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> MessageResponse:
        service.soft_delete(identity(principal), workspace_id)
        return MessageResponse(message="工作区已移入回收站，可在 30 天内恢复")

    @router.post("/workspaces/{workspace_id}/restore", response_model=WorkspaceResponse)
    def restore_workspace(
        workspace_id: int,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> WorkspaceResponse:
        return _response(service.restore(identity(principal), workspace_id))

    @router.post("/admin/workspaces/cleanup", response_model=WorkspaceCleanupResponse)
    def cleanup_workspaces(
        admin: AdminContext = Depends(require_admin),
    ) -> WorkspaceCleanupResponse:
        result = service.cleanup_expired(admin)
        return WorkspaceCleanupResponse(deleted=result.deleted, skipped=result.skipped)

    app.include_router(router)

    @app.exception_handler(WorkspaceNotFoundError)
    def handle_workspace_not_found(_request: Request, exc: WorkspaceNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "WORKSPACE_NOT_FOUND", "message": str(exc)},
        )

    @app.exception_handler(WorkspaceConflictError)
    def handle_workspace_conflict(_request: Request, exc: WorkspaceConflictError):
        return JSONResponse(
            status_code=409,
            content={"code": "WORKSPACE_CONFLICT", "message": str(exc)},
        )

    app.state.workspace_service = service
    return service
