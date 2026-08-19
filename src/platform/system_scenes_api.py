from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from .admin_access import (
    create_require_platform_admin,
    enforce_admin_rate_limit,
    require_admin_console_enabled,
)
from .admin_contracts import ADMIN_DEFAULT_PAGE_SIZE, ADMIN_MAX_PAGE_SIZE, SystemScenePayload
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .contracts import AdminContext, UserContext, WorkspaceContext
from .error_protocol import request_correlation_id
from .identifiers import parse_database_id
from .media_storage import CloudMediaStorage, MediaStorageError, MediaValidationError
from .observability import metrics
from .settings import DeploymentSettings
from .system_scenes import (
    SystemSceneCatalogService,
    SystemSceneConflictError,
    SystemSceneNotFoundError,
    SystemScenePage,
    SystemSceneView,
)


class SystemSceneCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene: SystemScenePayload
    reason: str = Field(min_length=1, max_length=2000)


class SystemSceneUpdateRequest(SystemSceneCreateRequest):
    expected_version: PositiveInt
    confirmed_usage_count: int | None = Field(default=None, ge=0)


class SystemSceneActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: PositiveInt
    reason: str = Field(min_length=1, max_length=2000)
    confirmed_usage_count: int | None = Field(default=None, ge=0)


class SystemSceneVisibilityRequest(SystemSceneActionRequest):
    visibility: Literal["enabled", "disabled"]


class SystemSceneReorderRequest(SystemSceneActionRequest):
    sort_order: int = Field(ge=0, le=1_000_000)


class SystemSceneCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int | None = Field(default=None, ge=1)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _scene_response(view: SystemSceneView, *, include_usage: bool) -> dict[str, Any]:
    record = view.record
    payload = view.payload.model_dump(mode="json")
    return {
        "id": str(record.id),
        **payload,
        "version": record.version,
        "lifecycle": "archived" if record.deleted_at is not None else "active",
        "usage_count": view.usage_count if include_usage else None,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "archived_at": _iso(record.deleted_at),
    }


def _page_response(page: SystemScenePage) -> dict[str, Any]:
    return {
        "items": [_scene_response(item, include_usage=True) for item in page.items],
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
    }


def install_cloud_system_scenes_api(
    app: FastAPI,
    auth: AuthApplication,
    settings: DeploymentSettings,
    media_storage: CloudMediaStorage,
) -> SystemSceneCatalogService:
    service = SystemSceneCatalogService(auth.database, media_storage)
    admin_router = APIRouter(prefix="/admin/system-scenes", tags=["系统场景管理"])
    catalog_router = APIRouter(prefix="/system-scenes", tags=["系统场景目录"])
    require_admin = create_require_platform_admin(auth.admin_sessions)

    def require_console(identity: AdminContext = Depends(require_admin)) -> AdminContext:
        require_admin_console_enabled(settings)
        return identity

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

    def require_identity(
        principal: SessionPrincipal = Depends(require_principal),
    ) -> UserContext:
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
        )

    def require_workspace_context(
        workspace_id: str = Header(alias="X-Workspace-ID"),
        identity: UserContext = Depends(require_identity),
    ) -> WorkspaceContext:
        return WorkspaceContext(
            identity=identity,
            workspace_id=str(parse_database_id(workspace_id, field="工作区 ID")),
        )

    @admin_router.get("")
    def list_admin_scenes(
        scene_id: int | None = Query(default=None, ge=1),
        query: str | None = Query(default=None, max_length=120),
        category: str | None = Query(default=None, max_length=80),
        tag: str | None = Query(default=None, max_length=40),
        visibility: Literal["enabled", "disabled"] | None = Query(default=None),
        lifecycle: Literal["active", "archived"] | None = Query(default=None),
        schema_version: int | None = Query(default=None, ge=1, le=100),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=ADMIN_DEFAULT_PAGE_SIZE, ge=1, le=ADMIN_MAX_PAGE_SIZE),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return _page_response(
            service.list_admin(
                identity,
                scene_id=scene_id,
                query=query,
                category=category,
                tag=tag,
                visibility=visibility,
                lifecycle=lifecycle,
                schema_version=schema_version,
                start_at=start_at,
                end_at=end_at,
                offset=offset,
                limit=limit,
            )
        )

    @admin_router.post("", status_code=201)
    def create_scene(
        payload: SystemSceneCreateRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="system_scene_create")
        result = service.create(
            identity,
            payload.scene,
            reason=payload.reason,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_system_scene_operations_total", labels={"action": "create"})
        return _scene_response(result, include_usage=True)

    @admin_router.post("/{scene_id}/reorder")
    def reorder_scene(
        scene_id: PositiveInt,
        payload: SystemSceneReorderRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(
            auth,
            settings,
            request,
            identity,
            action="system_scene_reorder",
        )
        current = service.get(identity, scene_id, admin_view=True)
        result = service.update(
            identity,
            scene_id,
            current.payload.model_copy(update={"sort_order": payload.sort_order}),
            expected_version=payload.expected_version,
            reason=payload.reason,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment(
            "admin_system_scene_operations_total",
            labels={"action": "reorder"},
        )
        return _scene_response(result, include_usage=True)

    @admin_router.post("/media", status_code=201)
    async def upload_scene_media(
        request: Request,
        file: UploadFile = File(...),
        reason: str = Form(min_length=1, max_length=2000),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="system_scene_media_upload")
        content = await file.read(SystemSceneCatalogService.MAX_COVER_BYTES + 1)
        stored = service.store_system_media(
            identity,
            content=content,
            mime_type=file.content_type or "application/octet-stream",
            filename=file.filename or "cover",
            reason=reason,
        )
        metrics.increment("admin_system_scene_operations_total", labels={"action": "media_upload"})
        return {
            "media_id": str(stored.media_id),
            "mime_type": stored.mime_type,
            "size_bytes": stored.size_bytes,
            "checksum_sha256": stored.checksum_sha256,
            "correlation_id": request_correlation_id(request),
        }

    @admin_router.get("/media/{media_id}/access")
    def get_admin_system_media_access(
        media_id: PositiveInt,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        url, expires_at = service.authorized_admin_media_url(identity, media_id)
        return {
            "media_id": str(media_id),
            "url": url,
            "expires_at": expires_at.isoformat(),
        }

    @admin_router.get("/{scene_id}")
    def get_admin_scene(
        scene_id: PositiveInt,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return _scene_response(service.get(identity, scene_id, admin_view=True), include_usage=True)

    @admin_router.put("/{scene_id}")
    def update_scene(
        scene_id: PositiveInt,
        payload: SystemSceneUpdateRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="system_scene_update")
        result = service.update(
            identity,
            scene_id,
            payload.scene,
            expected_version=payload.expected_version,
            reason=payload.reason,
            confirmed_usage_count=payload.confirmed_usage_count,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_system_scene_operations_total", labels={"action": "update"})
        return _scene_response(result, include_usage=True)

    @admin_router.post("/{scene_id}/visibility")
    def set_scene_visibility(
        scene_id: PositiveInt,
        payload: SystemSceneVisibilityRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="system_scene_visibility")
        current = service.get(identity, scene_id, admin_view=True)
        scene = current.payload.model_copy(update={"visibility": payload.visibility})
        result = service.update(
            identity,
            scene_id,
            scene,
            expected_version=payload.expected_version,
            reason=payload.reason,
            confirmed_usage_count=payload.confirmed_usage_count,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_system_scene_operations_total", labels={"action": payload.visibility})
        return _scene_response(result, include_usage=True)

    @admin_router.post("/{scene_id}/archive")
    def archive_scene(
        scene_id: PositiveInt,
        payload: SystemSceneActionRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="system_scene_archive")
        current = service.get(identity, scene_id, admin_view=True)
        result = service.update(
            identity,
            scene_id,
            current.payload,
            expected_version=payload.expected_version,
            reason=payload.reason,
            confirmed_usage_count=payload.confirmed_usage_count,
            archive=True,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_system_scene_operations_total", labels={"action": "archive"})
        return _scene_response(result, include_usage=True)

    @catalog_router.get("")
    def list_catalog(
        identity: UserContext = Depends(require_identity),
    ) -> dict[str, Any]:
        items = service.list_enabled(identity)
        return {"items": [_scene_response(item, include_usage=False) for item in items]}

    @catalog_router.get("/media/{media_id}/access")
    def get_system_media_access(
        media_id: PositiveInt,
        identity: UserContext = Depends(require_identity),
    ) -> dict[str, Any]:
        url, expires_at = service.authorized_media_url(identity, media_id)
        return {"media_id": str(media_id), "url": url, "expires_at": expires_at.isoformat()}

    @catalog_router.get("/{scene_id}")
    def get_catalog_scene(
        scene_id: PositiveInt,
        identity: UserContext = Depends(require_identity),
    ) -> dict[str, Any]:
        return _scene_response(service.get(identity, scene_id, admin_view=False), include_usage=False)

    @catalog_router.post("/{scene_id}/copy", status_code=201)
    def copy_catalog_scene(
        scene_id: PositiveInt,
        payload: SystemSceneCopyRequest,
        context: WorkspaceContext = Depends(require_workspace_context),
    ) -> dict[str, Any]:
        record = service.copy_to_workspace(context, scene_id, project_id=payload.project_id)
        return {
            "asset_record_id": str(record.id),
            "asset_id": str(record.payload.get("id") or ""),
            "scope": record.scope,
            "project_id": str(record.project_id) if record.project_id else None,
            "workspace_id": str(record.workspace_id),
            "source_system_scene_id": str(scene_id),
            "source_version": record.provenance.get("source_version"),
            "version": record.version,
        }

    app.include_router(admin_router)
    app.include_router(catalog_router)
    app.state.system_scene_catalog_service = service

    @app.exception_handler(SystemSceneNotFoundError)
    def handle_scene_not_found(request: Request, exc: SystemSceneNotFoundError):
        return JSONResponse(
            status_code=404,
            content={
                "code": "SYSTEM_SCENE_NOT_FOUND",
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    @app.exception_handler(SystemSceneConflictError)
    def handle_scene_conflict(request: Request, exc: SystemSceneConflictError):
        return JSONResponse(
            status_code=422 if exc.code == "REASON_INVALID" else 409,
            content={
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
                "correlation_id": request_correlation_id(request),
            },
        )

    @app.exception_handler(MediaValidationError)
    def handle_media_validation(request: Request, exc: MediaValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "SYSTEM_MEDIA_INVALID",
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    @app.exception_handler(MediaStorageError)
    def handle_media_storage(request: Request, exc: MediaStorageError):
        return JSONResponse(
            status_code=503,
            content={
                "code": "SYSTEM_MEDIA_UNAVAILABLE",
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    return service
