from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .ai_gateway_api import (
    AITaskSubmitter,
    CloudAIRequestAdapter,
    require_idempotency_key,
    submit_ai_task,
)
from .ai_request_policy import enforce_cloud_ai_request
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .content_repositories import (
    InvalidRepositoryContextError,
    OptimisticVersionConflictError,
    ScopedDocumentNotFoundError,
)
from .contracts import MediaWrite, UserContext, VersionedDocument, WorkspaceContext
from .media_storage import CloudMediaStorage, MediaValidationError
from .identifiers import parse_database_id
from .storyboard_service import CloudStoryboardService, StoryboardValidationError


MAX_FRAME_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_FRAME_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class StoryboardVersionRequiredError(ValueError):
    pass


class UpdateFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: str = Field(min_length=1)
    image_prompt: str | None = None
    action_description: str | None = None
    dialogue: str | None = None
    camera_angle: str | None = None
    scene_id: str | None = None
    character_ids: list[str] | None = None
    prop_ids: list[str] | None = None
    duration: int | None = Field(default=None, ge=1)
    shot_size: str | None = None
    camera_movement_description: str | None = None
    transition_hint: str | None = None


class AddFrameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str | None = None
    action_description: str = ""
    camera_angle: str = "中景"
    insert_at: int | None = Field(default=None, ge=0)


class ReplaceGeneratedFramesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frames: list[dict[str, Any]] = Field(min_length=1)


class FrameTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: str = Field(min_length=1)


class CopyFrameRequest(FrameTargetRequest):
    insert_at: int | None = Field(default=None, ge=0)


class ReorderFramesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_ids: list[str] = Field(min_length=1)


class UpdateFrameWorkbenchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workbench_tab_mode: Literal["t2i_i2v", "direct_r2v"] | None = None
    t2i_selected_index: int | None = Field(default=None, ge=0)
    workbench_generate_count: int | None = Field(default=None, ge=1, le=6)
    t2i_image_urls: list[str] | None = Field(default=None, max_length=10)


class AttachFrameMediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    media_kind: Literal[
        "rendered_image",
        "t2i_image",
        "video",
        "audio",
        "sfx",
        "dubbed_video",
        "background_audio",
        "preview_video",
    ]
    media_id: str = Field(min_length=1)


class AttachGeneratedVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    media_id: str = Field(min_length=1)
    prompt: str = ""
    image_url: str = ""
    duration: int = Field(default=5, ge=1, le=600)
    resolution: str = Field(default="720p", min_length=1, max_length=32)
    model: str = Field(default="server-selected", min_length=1, max_length=160)
    generation_mode: Literal["i2v", "r2v"] = "i2v"
    workbench_tab: Literal["t2i_i2v", "direct_r2v"] | None = None


class SelectFrameVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(min_length=1)


class AudioMixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    background_music_media_id: str | None = Field(default=None, min_length=1)
    dialogue_volume: int | None = Field(default=None, ge=0, le=100)
    bgm_volume: int | None = Field(default=None, ge=0, le=100)
    sfx_volume: int | None = Field(default=None, ge=0, le=100)


def _content_response(stored: VersionedDocument[Any]) -> dict[str, Any]:
    payload = stored.document.model_dump(mode="json")
    payload["version"] = stored.version
    payload["schema_version"] = stored.schema_version
    return payload


def _frame_response(
    stored: VersionedDocument[Any],
    frame_id: str,
) -> dict[str, Any]:
    frame = next(item for item in stored.document.frames if item.id == frame_id)
    payload = frame.model_dump(mode="json")
    payload["version"] = stored.version
    return payload


def _parse_expected_version(request: Request) -> int:
    raw_value = request.headers.get("if-match")
    if not raw_value:
        raise StoryboardVersionRequiredError("此操作需要 If-Match 内容版本")
    normalized = raw_value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    try:
        version = int(normalized)
    except ValueError as exc:
        raise StoryboardVersionRequiredError("If-Match 内容版本无效") from exc
    if version < 1:
        raise StoryboardVersionRequiredError("If-Match 内容版本无效")
    return version


def _read_frame_image(file: UploadFile) -> bytes:
    if (file.content_type or "").lower() not in ALLOWED_FRAME_IMAGE_TYPES:
        raise MediaValidationError("分镜图片仅支持 JPG、PNG 或 WebP 格式")
    content = file.file.read(MAX_FRAME_IMAGE_BYTES + 1)
    if len(content) > MAX_FRAME_IMAGE_BYTES:
        raise MediaValidationError("单张分镜图片不能超过 8 MB")
    if not content:
        raise MediaValidationError("分镜图片不能为空")
    return content


def install_cloud_storyboard_api(
    app: FastAPI,
    auth: AuthApplication,
    media_storage: CloudMediaStorage,
    *,
    ai_submitter: AITaskSubmitter | None = None,
) -> CloudStoryboardService:
    service = CloudStoryboardService(auth.database, media_storage)
    router = APIRouter(tags=["工作区分镜"])
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

    @router.post("/projects/{project_id}/frames/update")
    def update_frame(
        project_id: str,
        payload: UpdateFrameRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        updates = payload.model_dump(
            exclude={"frame_id", "camera_movement_description"},
            exclude_unset=True,
        )
        if "camera_movement_description" in payload.model_fields_set:
            updates["camera_movement"] = payload.camera_movement_description
        return _content_response(
            service.update_frame(
                context,
                project_id,
                payload.frame_id,
                updates,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames", status_code=201)
    def add_frame(
        project_id: str,
        payload: AddFrameRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.add_frame(
                context,
                project_id,
                scene_id=payload.scene_id or "",
                action_description=payload.action_description,
                camera_angle=payload.camera_angle,
                insert_at=payload.insert_at,
                expected_version=_parse_expected_version(request),
            )
        )

    @router.put("/projects/{project_id}/frames")
    def replace_generated_frames(
        project_id: str,
        payload: ReplaceGeneratedFramesRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.replace_generated_frames(
                context,
                project_id,
                payload.frames,
                _parse_expected_version(request),
            )
        )

    @router.delete("/projects/{project_id}/frames/{frame_id}")
    def delete_frame(
        project_id: str,
        frame_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.delete_frame(
                context,
                project_id,
                frame_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames/copy")
    def copy_frame(
        project_id: str,
        payload: CopyFrameRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.copy_frame(
                context,
                project_id,
                payload.frame_id,
                payload.insert_at,
                _parse_expected_version(request),
            )
        )

    @router.put("/projects/{project_id}/frames/reorder")
    def reorder_frames(
        project_id: str,
        payload: ReorderFramesRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.reorder_frames(
                context,
                project_id,
                payload.frame_ids,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames/toggle_lock")
    def toggle_frame_lock(
        project_id: str,
        payload: FrameTargetRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.toggle_frame_lock(
                context,
                project_id,
                payload.frame_id,
                _parse_expected_version(request),
            )
        )

    @router.patch("/projects/{project_id}/frames/{frame_id}/workbench")
    def update_frame_workbench(
        project_id: str,
        frame_id: str,
        payload: UpdateFrameWorkbenchRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        stored = service.update_workbench(
            context,
            project_id,
            frame_id,
            payload.model_dump(exclude_unset=True),
            _parse_expected_version(request),
        )
        return _frame_response(stored, frame_id)

    @router.post("/projects/{project_id}/frames/{frame_id}/media")
    def attach_frame_media(
        project_id: str,
        frame_id: str,
        payload: AttachFrameMediaRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.attach_frame_media(
                context,
                project_id,
                frame_id,
                payload.media_kind,
                payload.media_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames/{frame_id}/video_candidates")
    def attach_generated_video(
        project_id: str,
        frame_id: str,
        payload: AttachGeneratedVideoRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.attach_generated_video(
                context,
                project_id,
                frame_id,
                task_id=payload.task_id,
                media_id=payload.media_id,
                prompt=payload.prompt,
                image_url=payload.image_url,
                duration=payload.duration,
                resolution=payload.resolution,
                model=payload.model,
                generation_mode=payload.generation_mode,
                workbench_tab=payload.workbench_tab,
                expected_version=_parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames/{frame_id}/select_video")
    def select_frame_video(
        project_id: str,
        frame_id: str,
        payload: SelectFrameVideoRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.select_frame_video(
                context,
                project_id,
                frame_id,
                payload.video_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames/{frame_id}/auto_select_latest_video")
    def auto_select_latest_video(
        project_id: str,
        frame_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.auto_select_latest_video(
                context,
                project_id,
                frame_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames/{frame_id}/unpin_video")
    def unpin_frame_video(
        project_id: str,
        frame_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.unpin_frame_video(
                context,
                project_id,
                frame_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/merge")
    def merge_project_videos(
        project_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.merge_videos(
                context,
                project_id,
                _parse_expected_version(request),
            )
        )

    def store_uploaded_frame_image(
        *,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        media_kind: Literal["rendered_image", "t2i_image"],
        file: UploadFile,
        expected_version: int,
    ) -> VersionedDocument[Any]:
        service.validate_frame_update(
            context,
            project_id,
            frame_id,
            expected_version,
        )
        content = _read_frame_image(file)
        stored_media = media_storage.store(
            context,
            MediaWrite(
                content=content,
                content_type=file.content_type or "application/octet-stream",
                filename=file.filename or "frame-image",
                project_id=project_id,
                provenance={"origin": "storyboard_upload", "frame_id": frame_id},
            ),
        )
        try:
            return service.attach_frame_media(
                context,
                project_id,
                frame_id,
                media_kind,
                stored_media.media_id,
                expected_version,
            )
        except Exception:
            try:
                media_storage.delete(context, stored_media.media_id)
            except Exception:
                pass
            raise

    @router.post("/projects/{project_id}/frames/{frame_id}/upload_image")
    def upload_frame_image(
        project_id: str,
        frame_id: str,
        request: Request,
        file: UploadFile = File(...),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            store_uploaded_frame_image(
                context=context,
                project_id=project_id,
                frame_id=frame_id,
                media_kind="rendered_image",
                file=file,
                expected_version=_parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/frames/{frame_id}/upload_t2i")
    def upload_t2i_frame(
        project_id: str,
        frame_id: str,
        request: Request,
        file: UploadFile = File(...),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        stored = store_uploaded_frame_image(
            context=context,
            project_id=project_id,
            frame_id=frame_id,
            media_kind="t2i_image",
            file=file,
            expected_version=_parse_expected_version(request),
        )
        return _frame_response(stored, frame_id)

    @router.put("/projects/{project_id}/audio_mix")
    def update_audio_mix(
        project_id: str,
        payload: AudioMixRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        background_music_was_set = (
            "background_music_media_id" in payload.model_fields_set
        )
        return _content_response(
            service.update_audio_mix(
                context,
                project_id,
                background_music_media_id=payload.background_music_media_id,
                clear_background_music=(
                    background_music_was_set
                    and payload.background_music_media_id is None
                ),
                volume_updates={
                    "dialogue": payload.dialogue_volume,
                    "bgm": payload.bgm_volume,
                    "sfx": payload.sfx_volume,
                },
                expected_version=_parse_expected_version(request),
            )
        )

    async def gateway_not_ready(
        request: Request,
        _context: WorkspaceContext = Depends(require_context),
    ) -> None:
        await enforce_cloud_ai_request(request)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AI_GATEWAY_NOT_READY",
                "message": "云端 AI 网关尚未启用，未执行也未扣除算力券",
            },
        )

    @router.post("/projects/{project_id}/dialogue_audio/batch")
    async def generate_dialogue_audio_batch(
        project_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> JSONResponse:
        await enforce_cloud_ai_request(request)
        if ai_submitter is None:
            return await gateway_not_ready(request, context)
        content, stats = service.build_dialogue_batch_content(context, project_id)
        if not content["items"]:
            return JSONResponse(status_code=200, content={"_batch_stats": stats})
        submitted = submit_ai_task(
            ai_submitter,
            context,
            capability="speech.tts",
            idempotency_key=require_idempotency_key(request, {}),
            project_id=project_id,
            content=content,
        )
        return JSONResponse(status_code=202, content=submitted)

    ai_routes = [
        (
            "/projects/{project_id}/storyboard/analyze",
            "script.analysis",
            "storyboard.analyze",
        ),
        (
            "/projects/{project_id}/storyboard/refine_prompt",
            "prompt.polish",
            "storyboard.prompt.refine",
        ),
        (
            "/projects/{project_id}/storyboard/refine_batch",
            "prompt.polish",
            "storyboard.prompt.refine_batch",
        ),
        (
            "/projects/{project_id}/storyboard/render",
            "image.t2i",
            "storyboard.render",
        ),
        (
            "/projects/{project_id}/frames/{frame_id}/refine",
            "prompt.polish",
            "storyboard.frame.refine",
        ),
        (
            "/projects/{project_id}/generate_storyboard",
            "script.analysis",
            "storyboard.generate",
        ),
        (
            "/projects/{project_id}/generate_video",
            "video.i2v",
            "video.generate",
        ),
        (
            "/projects/{project_id}/generate_audio",
            "speech.tts",
            "audio.generate_dialogue",
        ),
        (
            "/projects/{project_id}/video_tasks",
            "video.i2v",
            "video.task.create",
        ),
        (
            "/projects/{project_id}/frames/{frame_id}/audio",
            "speech.tts",
            "audio.frame.generate",
        ),
        (
            "/projects/{project_id}/frames/{frame_id}/dub/preview",
            "speech.tts",
            "audio.dub.preview",
        ),
        (
            "/projects/{project_id}/frames/{frame_id}/dub/apply",
            "speech.tts",
            "audio.dub.apply",
        ),
        (
            "/projects/{project_id}/mix/generate_sfx",
            "audio.sfx",
            "audio.sfx.generate",
        ),
        (
            "/projects/{project_id}/mix/generate_bgm",
            "audio.sfx",
            "audio.bgm.generate",
        ),
        ("/video/polish_prompt", "prompt.polish", "video.prompt.polish"),
        ("/video/polish_r2v_prompt", "prompt.polish", "video.r2v_prompt.polish"),
    ]

    def add_ai_route(path: str, capability: str, operation: str) -> None:
        if ai_adapter is None:
            endpoint = gateway_not_ready
        else:
            async def endpoint(
                request: Request,
                context: WorkspaceContext = Depends(require_context),
            ) -> JSONResponse:
                return await ai_adapter.submit_request(
                    request,
                    context,
                    capability=capability,
                    operation=operation,
                )

        router.add_api_route(
            path,
            endpoint,
            methods=["POST"],
            name=f"cloud_ai_{operation.replace('.', '_')}",
        )

    for path, capability, operation in ai_routes:
        add_ai_route(path, capability, operation)

    blocked_non_gateway_routes = [
        ("/projects/{project_id}/video_tasks/{task_id}", ["GET"]),
        ("/projects/{project_id}/video_tasks/{task_id}/annotate", ["PATCH"]),
        ("/projects/{project_id}/video_tasks/{task_id}/cancel", ["POST"]),
        ("/tasks/{task_id}", ["GET"]),
        ("/projects/{project_id}/frames/{frame_id}/dub", ["DELETE"]),
        ("/projects/{project_id}/frames/{frame_id}/extract_last_frame", ["POST"]),
        ("/projects/{project_id}/export", ["POST"]),
    ]
    for index, (path, methods) in enumerate(blocked_non_gateway_routes):
        router.add_api_route(
            path,
            gateway_not_ready,
            methods=methods,
            name=f"cloud_ai_gateway_pending_{index}",
        )

    @router.get("/bgm/presets")
    def unavailable_bgm_catalog(
        _context: WorkspaceContext = Depends(require_context),
    ) -> None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MEDIA_CATALOG_NOT_READY",
                "message": "云端背景音乐库尚未启用",
            },
        )

    app.include_router(router)

    @app.exception_handler(StoryboardVersionRequiredError)
    def handle_version_required(
        _request: Request,
        exc: StoryboardVersionRequiredError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=428,
            content={"code": "CONTENT_VERSION_REQUIRED", "message": str(exc)},
        )

    @app.exception_handler(StoryboardValidationError)
    def handle_invalid_storyboard(
        _request: Request,
        exc: StoryboardValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "STORYBOARD_INVALID", "message": str(exc)},
        )

    @app.exception_handler(OptimisticVersionConflictError)
    def handle_storyboard_conflict(
        _request: Request,
        exc: OptimisticVersionConflictError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "CONTENT_VERSION_CONFLICT", "message": str(exc)},
        )

    @app.exception_handler(ScopedDocumentNotFoundError)
    def handle_storyboard_not_found(
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

    app.state.cloud_storyboard_service = service
    return service
