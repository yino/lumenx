from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .ai_gateway_api import AITaskSubmitter, CloudAIRequestAdapter
from .ai_request_policy import enforce_cloud_ai_request
from .asset_repositories import AssetConflictError
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .cloud_playground import CloudPlaygroundService, CloudPlaygroundValidationError
from .content_repositories import (
    DocumentPayloadValidationError,
    InvalidRepositoryContextError,
    OptimisticVersionConflictError,
    ScopedDocumentNotFoundError,
)
from .contracts import MediaWrite, UserContext, WorkspaceContext
from .media_storage import CloudMediaStorage, MediaValidationError
from .identifiers import parse_database_id


MAX_PLAYGROUND_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_PLAYGROUND_MEDIA_TYPES = {
    "audio/mpeg",
    "audio/wav",
    "image/jpeg",
    "image/png",
    "image/webp",
    "video/mp4",
    "video/quicktime",
    "video/webm",
}


class PlaygroundVersionRequiredError(ValueError):
    pass


class CreateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=240)
    category: Literal["image", "video", "general"] = "general"
    prompt: str = Field(min_length=1)
    negative_prompt: str | None = None
    default_mode: Literal["t2i", "i2i", "t2v", "i2v", "r2v", "v2v"] | None = None
    default_parameters: dict[str, Any] = Field(default_factory=dict)


class UpdateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=240)
    category: Literal["image", "video", "general"] | None = None
    prompt: str | None = Field(default=None, min_length=1)
    negative_prompt: str | None = None
    default_mode: Literal["t2i", "i2i", "t2v", "i2v", "r2v", "v2v"] | None = None
    default_parameters: dict[str, Any] | None = None


class SaveToLibraryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal["character", "scene", "prop"] = "prop"


def _parse_expected_version(request: Request) -> int:
    raw_value = request.headers.get("if-match")
    if not raw_value:
        raise PlaygroundVersionRequiredError("此操作需要 If-Match 模板版本")
    normalized = raw_value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    try:
        version = int(normalized)
    except ValueError as exc:
        raise PlaygroundVersionRequiredError("If-Match 模板版本无效") from exc
    if version < 1:
        raise PlaygroundVersionRequiredError("If-Match 模板版本无效")
    return version


def install_cloud_playground_api(
    app: FastAPI,
    auth: AuthApplication,
    media_storage: CloudMediaStorage,
    *,
    ai_submitter: AITaskSubmitter | None = None,
) -> CloudPlaygroundService:
    service = CloudPlaygroundService(auth.database)
    router = APIRouter(prefix="/playground", tags=["工作区创作实验室"])
    ai_adapter = (
        CloudAIRequestAdapter(ai_submitter) if ai_submitter is not None else None
    )

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

    @router.post("/generate")
    async def generate(
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> JSONResponse:
        if ai_adapter is None:
            await enforce_cloud_ai_request(request)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "AI_GATEWAY_NOT_READY",
                    "message": "云端 AI 网关尚未启用，未执行也未扣除算力券",
                },
            )
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        mode = str(payload.get("mode") or "t2i") if isinstance(payload, dict) else "t2i"
        capabilities = {
            "t2i": "image.t2i",
            "i2i": "image.i2i",
            "t2v": "video.t2v",
            "i2v": "video.i2v",
            "r2v": "video.r2v",
            "v2v": "video.v2v",
        }
        capability = capabilities.get(mode)
        if capability is None:
            raise CloudPlaygroundValidationError("创作模式无效")
        return await ai_adapter.submit_request(
            request,
            context,
            capability=capability,
            operation=f"playground.{mode}",
        )

    @router.get("/history")
    def list_history(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        context: WorkspaceContext = Depends(require_context),
    ) -> list[dict[str, Any]]:
        return service.list_history(context, limit=limit, offset=offset)

    @router.get("/history/{generation_id}")
    def get_generation(
        generation_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return service.get_generation(context, generation_id)

    @router.get("/history/{generation_id}/status")
    def get_generation_status(
        generation_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        generation = service.get_generation(context, generation_id)
        return {
            "id": generation["id"],
            "status": generation["status"],
            "raw_status": generation["raw_status"],
            "status_zh": generation["status_zh"],
            "quoted_microtickets": generation["quoted_microtickets"],
            "quoted_tickets": generation["quoted_tickets"],
            "tokens_per_ticket": generation["tokens_per_ticket"],
            "cancellation_requested": generation["cancellation_requested"],
            "support_review": generation["support_review"],
            "outputs": generation["outputs"],
            "error_code": generation["error_code"],
            "error": generation["error"],
        }

    @router.delete("/history/{generation_id}")
    def delete_generation(
        generation_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, bool]:
        service.hide_generation(context, generation_id)
        return {"ok": True}

    @router.post("/history/{generation_id}/outputs/{output_id}/save-to-library")
    def save_to_library(
        generation_id: str,
        output_id: str,
        payload: SaveToLibraryRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        asset = service.save_output_to_library(
            context,
            generation_id,
            output_id,
            payload.category,
        )
        return {
            "ok": True,
            "asset_id": asset.domain_id,
            "media_id": asset.media_object_id,
        }

    @router.get("/templates")
    def list_templates(
        context: WorkspaceContext = Depends(require_context),
    ) -> list[dict[str, Any]]:
        return service.list_templates(context)

    @router.post("/templates", status_code=201)
    def create_template(
        payload: CreateTemplateRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return service.create_template(
            context,
            payload.model_dump(mode="json"),
        )

    @router.put("/templates/{template_id}")
    def update_template(
        template_id: str,
        payload: UpdateTemplateRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return service.update_template(
            context,
            template_id,
            payload.model_dump(mode="json", exclude_unset=True),
            _parse_expected_version(request),
        )

    @router.delete("/templates/{template_id}")
    def delete_template(
        template_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, bool]:
        service.delete_template(
            context,
            template_id,
            _parse_expected_version(request),
        )
        return {"ok": True}

    @router.post("/upload", status_code=201)
    def upload_media(
        file: UploadFile = File(...),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        content_type = (file.content_type or "").lower()
        if content_type not in ALLOWED_PLAYGROUND_MEDIA_TYPES:
            raise MediaValidationError("创作素材格式不受支持")
        content = file.file.read(MAX_PLAYGROUND_UPLOAD_BYTES + 1)
        if len(content) > MAX_PLAYGROUND_UPLOAD_BYTES:
            raise MediaValidationError("单个创作素材不能超过 50 MB")
        stored = media_storage.store(
            context,
            MediaWrite(
                content=content,
                content_type=content_type,
                filename=file.filename or "playground-upload",
                provenance={"origin": "playground_upload"},
            ),
        )
        return {
            "id": stored.media_id,
            "mime_type": stored.content_type,
            "size_bytes": stored.size_bytes,
            "checksum_sha256": stored.checksum_sha256,
        }

    app.include_router(router)

    @app.exception_handler(PlaygroundVersionRequiredError)
    def handle_version_required(
        _request: Request,
        exc: PlaygroundVersionRequiredError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=428,
            content={"code": "CONTENT_VERSION_REQUIRED", "message": str(exc)},
        )

    @app.exception_handler(CloudPlaygroundValidationError)
    def handle_invalid_playground(
        _request: Request,
        exc: CloudPlaygroundValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "PLAYGROUND_INVALID", "message": str(exc)},
        )

    @app.exception_handler(OptimisticVersionConflictError)
    def handle_playground_conflict(
        _request: Request,
        exc: OptimisticVersionConflictError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "CONTENT_VERSION_CONFLICT", "message": str(exc)},
        )

    @app.exception_handler(DocumentPayloadValidationError)
    @app.exception_handler(AssetConflictError)
    def handle_invalid_template(
        _request: Request,
        exc: Exception,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "PLAYGROUND_TEMPLATE_INVALID", "message": str(exc)},
        )

    @app.exception_handler(ScopedDocumentNotFoundError)
    def handle_playground_not_found(
        _request: Request,
        _exc: ScopedDocumentNotFoundError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"code": "CONTENT_NOT_FOUND", "message": "资源不存在"},
        )

    @app.exception_handler(InvalidRepositoryContextError)
    def handle_invalid_context(
        _request: Request,
        exc: InvalidRepositoryContextError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "WORKSPACE_CONTEXT_INVALID", "message": str(exc)},
        )

    app.state.cloud_playground_service = service
    return service
