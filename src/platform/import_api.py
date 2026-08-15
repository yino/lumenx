from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, PositiveInt

from .auth.admin import AdminAuthorizationError
from .audit import AuditService
from .auth.api import AuthApplication
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .contracts import UserContext
from .local_import import (
    LocalImportConflictError,
    LocalImportService,
    LocalImportValidationError,
)
from .media_storage import CloudMediaStorage


class ImportDryRunRequest(BaseModel):
    target_user_id: PositiveInt
    target_workspace_id: PositiveInt
    source_directory: str = Field(min_length=1, max_length=512)
    include_playground: bool = True
    missing_media_policy: Literal["reject", "clear"] = "reject"


class ImportRollbackRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def install_cloud_import_api(
    app: FastAPI,
    auth: AuthApplication,
    media_storage: CloudMediaStorage,
    *,
    allowed_root: str | Path,
    max_import_bytes: int = 10 * 1024 * 1024 * 1024,
) -> LocalImportService:
    service = LocalImportService(
        auth.database,
        media_storage,
        allowed_root=allowed_root,
        max_import_bytes=max_import_bytes,
    )
    audit = AuditService(auth.database)
    router = APIRouter(prefix="/admin/imports", tags=["本地数据导入"])

    def require_admin(request: Request) -> UserContext:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        principal: SessionPrincipal = auth.sessions.resolve(token)
        if not principal.is_platform_admin:
            raise AdminAuthorizationError("仅平台管理员可以执行本地数据导入")
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=True,
        )

    def execute_service(operation):
        try:
            return operation()
        except LocalImportValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "IMPORT_VALIDATION_FAILED", "message": str(exc)},
            ) from exc
        except LocalImportConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "IMPORT_CONFLICT", "message": str(exc)},
            ) from exc

    @router.post("/dry-run")
    def dry_run(
        payload: ImportDryRunRequest,
        request: Request,
        admin: UserContext = Depends(require_admin),
    ) -> dict:
        result = execute_service(
            lambda: service.dry_run(
                admin,
                target_user_id=str(payload.target_user_id),
                target_workspace_id=str(payload.target_workspace_id),
                source_directory=payload.source_directory,
                include_playground=payload.include_playground,
                missing_media_policy=payload.missing_media_policy,
            )
        )
        audit.record(
            admin,
            action="import.dry_run",
            target_type="import_batch",
            target_id=result["batch_id"],
            target_user_id=str(payload.target_user_id),
            workspace_id=str(payload.target_workspace_id),
            request=request,
            after={
                "ready": bool(result["ready"]),
                "issue_count": len(result["issues"]),
                "media_bytes": int(result["media_bytes"]),
            },
        )
        return result

    @router.post("/{batch_id}/execute")
    def execute(
        batch_id: str,
        request: Request,
        admin: UserContext = Depends(require_admin),
    ) -> dict:
        result = execute_service(lambda: service.execute(admin, batch_id))
        detail = execute_service(lambda: service.get_batch(admin, batch_id))
        audit.record(
            admin,
            action="import.execute",
            target_type="import_batch",
            target_id=batch_id,
            target_user_id=detail["target_user_id"],
            workspace_id=detail["target_workspace_id"],
            request=request,
            after={
                "status": result["status"],
                "integrity_ok": bool(result["integrity_ok"]),
                "item_status_counts": result["item_status_counts"],
            },
        )
        return result

    @router.get("/{batch_id}")
    def get_batch(batch_id: str, admin: UserContext = Depends(require_admin)) -> dict:
        return execute_service(lambda: service.get_batch(admin, batch_id))

    @router.post("/{batch_id}/rollback")
    def rollback(
        batch_id: str,
        payload: ImportRollbackRequest,
        request: Request,
        admin: UserContext = Depends(require_admin),
    ) -> dict:
        detail = execute_service(lambda: service.get_batch(admin, batch_id))
        result = execute_service(lambda: service.rollback(admin, batch_id, payload.reason))
        audit.record(
            admin,
            action="import.rollback.confirmed",
            target_type="import_batch",
            target_id=batch_id,
            target_user_id=detail["target_user_id"],
            workspace_id=detail["target_workspace_id"],
            request=request,
            reason=payload.reason,
            after={
                "reverted_items": result["reverted_items"],
                "preserved_items": result["preserved_items"],
            },
        )
        return result

    app.include_router(router)
    app.state.local_import_service = service
    return service
