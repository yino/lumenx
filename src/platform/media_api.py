from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .auth.api import AuthApplication
from .audit import AuditService
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .content_repositories import (
    InvalidRepositoryContextError,
    ScopedDocumentNotFoundError,
)
from .contracts import MediaWrite, UserContext, WorkspaceContext
from .media_storage import (
    CloudMediaStorage,
    MediaLifecycleConflictError,
    MediaStorageError,
    MediaValidationError,
    OSSPrivateObjectStore,
    PrivateObjectStore,
)
from .error_protocol import request_correlation_id
from .identifiers import parse_database_id
from .observability import events, metrics
from .runtime_policy import RuntimePolicyResolver
from .settings import DeploymentSettings


MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _media_response(media: Any) -> dict[str, Any]:
    return {
        "id": media.media_id,
        "mime_type": media.content_type,
        "size_bytes": media.size_bytes,
        "checksum_sha256": media.checksum_sha256,
    }


def install_cloud_media_api(
    app: FastAPI,
    auth: AuthApplication,
    settings: DeploymentSettings,
    *,
    object_store: PrivateObjectStore | None = None,
    storage: CloudMediaStorage | None = None,
) -> CloudMediaStorage:
    storage = storage or CloudMediaStorage(
        auth.database,
        object_store or OSSPrivateObjectStore(settings),
    )
    audit = AuditService(auth.database)
    runtime_policy: RuntimePolicyResolver | None = getattr(
        auth, "runtime_policy", None
    )
    router = APIRouter(tags=["工作区媒体"])

    def require_principal(request: Request) -> SessionPrincipal:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf_token = (
            request.headers.get("x-csrf-token")
            if request.method in UNSAFE_METHODS
            else None
        )
        return auth.sessions.resolve(token, csrf_token=csrf_token)

    def require_context(
        request: Request,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> WorkspaceContext:
        workspace_id = request.headers.get("x-workspace-id", "").strip()
        try:
            canonical_workspace_id = str(
                parse_database_id(workspace_id, field="工作区 ID")
            )
        except ValueError as exc:
            raise InvalidRepositoryContextError("请选择有效的工作区") from exc
        return WorkspaceContext(
            identity=UserContext(
                user_id=str(principal.user_id),
                session_id=str(principal.session_id),
            ),
            workspace_id=canonical_workspace_id,
        )

    @router.post("/media", status_code=201)
    @router.post("/upload", status_code=201)
    @router.post("/library/assets/upload", status_code=201)
    def upload_media(
        file: UploadFile = File(...),
        project_id: str | None = Query(default=None),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        content = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise MediaValidationError("单个媒体文件不能超过 50 MB")
        stored = storage.store(
            context,
            MediaWrite(
                content=content,
                content_type=file.content_type or "application/octet-stream",
                filename=file.filename or "upload.bin",
                project_id=project_id,
                provenance={"origin": "upload"},
            ),
        )
        metrics.increment(
            "media_operations_total",
            labels={"operation": "upload", "outcome": "succeeded"},
        )
        metrics.observe(
            "media_size_bytes",
            stored.size_bytes,
            labels={"operation": "upload"},
        )
        return _media_response(stored)

    @router.get("/media/{media_id}")
    def get_media_metadata(
        media_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        record = storage.repository.require(context, media_id)
        return {
            "id": str(record.id),
            "project_id": str(record.project_id) if record.project_id else None,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "checksum_sha256": record.checksum_sha256,
            "lifecycle_state": record.lifecycle_state,
            "provenance": dict(record.provenance),
            "created_at": record.created_at.isoformat(),
        }

    @router.get("/media/{media_id}/download")
    def download_media(
        media_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> Response:
        record = storage.repository.require(context, media_id)
        content = storage.read_bytes(context, media_id)
        suffix = {
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "video/mp4": ".mp4",
        }.get(record.mime_type, "")
        metrics.increment(
            "media_operations_total",
            labels={"operation": "download", "outcome": "succeeded"},
        )
        metrics.observe(
            "media_size_bytes",
            len(content),
            labels={"operation": "download"},
        )
        return Response(
            content=content,
            media_type=record.mime_type,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": (
                    f'attachment; filename="media-{media_id}{suffix}"'
                ),
                "Content-Length": str(len(content)),
            },
        )

    @router.get("/media/{media_id}/access")
    def get_media_access(
        media_id: str,
        expires_seconds: int = Query(default=300, ge=1, le=900),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        config_version_id = None
        effective_seconds = expires_seconds
        if runtime_policy is not None:
            policy = runtime_policy.resolve()
            config_version_id = policy.config_version_id
            effective_seconds = min(
                expires_seconds,
                policy.signed_media_url_seconds,
            )
        expires_at = datetime.now(UTC) + timedelta(seconds=effective_seconds)
        return {
            "media_id": media_id,
            "url": storage.authorized_url(context, media_id, expires_at),
            "expires_at": expires_at.isoformat(),
            "policy_config_version_id": config_version_id,
        }

    @router.delete("/media/{media_id}")
    def delete_media(
        media_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        storage.delete(context, media_id)
        metrics.increment(
            "media_operations_total",
            labels={"operation": "delete", "outcome": "succeeded"},
        )
        audit.record(
            context.identity,
            action="media.delete",
            target_type="media_object",
            target_id=media_id,
            request=request,
            workspace_id=context.workspace_id,
            after={"lifecycle_state": "deleted"},
        )
        return {"status": "deleted", "id": media_id}

    app.include_router(router)

    @app.exception_handler(MediaValidationError)
    def handle_invalid_media(
        request: Request,
        exc: MediaValidationError,
    ) -> JSONResponse:
        metrics.increment(
            "media_errors_total",
            labels={"operation": "validate", "error_code": "MEDIA_INVALID"},
        )
        events.emit(
            "media.operation_failed",
            correlation_id=request_correlation_id(request),
            operation="validate",
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=422,
            content={"code": "MEDIA_INVALID", "message": str(exc)},
        )

    @app.exception_handler(MediaStorageError)
    def handle_media_storage_error(
        request: Request,
        exc: MediaStorageError,
    ) -> JSONResponse:
        metrics.increment(
            "media_errors_total",
            labels={"operation": "storage", "error_code": "MEDIA_STORAGE_UNAVAILABLE"},
        )
        events.emit(
            "media.operation_failed",
            correlation_id=request_correlation_id(request),
            operation="storage",
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"code": "MEDIA_STORAGE_UNAVAILABLE", "message": str(exc)},
        )

    @app.exception_handler(MediaLifecycleConflictError)
    def handle_media_conflict(
        request: Request,
        exc: MediaLifecycleConflictError,
    ) -> JSONResponse:
        metrics.increment(
            "media_errors_total",
            labels={"operation": "lifecycle", "error_code": "MEDIA_STATE_CONFLICT"},
        )
        events.emit(
            "media.operation_failed",
            correlation_id=request_correlation_id(request),
            operation="lifecycle",
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=409,
            content={"code": "MEDIA_STATE_CONFLICT", "message": str(exc)},
        )

    @app.exception_handler(ScopedDocumentNotFoundError)
    def handle_media_not_found(
        request: Request,
        _exc: ScopedDocumentNotFoundError,
    ) -> JSONResponse:
        metrics.increment(
            "media_errors_total",
            labels={"operation": "authorize", "error_code": "CONTENT_NOT_FOUND"},
        )
        events.emit(
            "media.access_denied",
            correlation_id=request_correlation_id(request),
            path=request.url.path,
        )
        return JSONResponse(
            status_code=404,
            content={"code": "CONTENT_NOT_FOUND", "message": "资源不存在"},
        )

    @app.exception_handler(InvalidRepositoryContextError)
    def handle_invalid_media_context(
        _request: Request,
        exc: InvalidRepositoryContextError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "WORKSPACE_CONTEXT_INVALID", "message": str(exc)},
        )

    app.state.cloud_media_storage = storage
    return storage
