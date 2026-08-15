from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.apps.comic_gen.llm import (
    DEFAULT_R2V_POLISH_PROMPT,
    DEFAULT_STORYBOARD_EXTRACTION_PROMPT,
    DEFAULT_STORYBOARD_POLISH_PROMPT,
    DEFAULT_VIDEO_POLISH_PROMPT,
    ScriptProcessor,
)
from src.apps.comic_gen.models import ArtDirection, PromptConfig

from .ai_gateway_api import (
    AITaskSubmitter,
    CloudAIRequestAdapter,
    require_idempotency_key,
    submit_ai_task,
)
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .content_repositories import (
    DocumentPayloadValidationError,
    InvalidRepositoryContextError,
    OptimisticVersionConflictError,
    ScopedDocumentNotFoundError,
)
from .content_service import CloudContentService
from .contracts import UserContext, VersionedDocument, WorkspaceContext
from .identifiers import parse_database_id


class ContentVersionRequiredError(ValueError):
    pass


class CloudContentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProjectRequest(CloudContentRequest):
    title: str = Field(min_length=1, max_length=240)
    text: str = ""
    workflow_mode: str = "r2v"
    series_id: str | None = None


class UpdateProjectTextRequest(CloudContentRequest):
    text: str = ""


class ReparseProjectRequest(CloudContentRequest):
    text: str = Field(min_length=1)


class CreateSeriesRequest(CloudContentRequest):
    title: str = Field(min_length=1, max_length=240)
    description: str = ""
    workflow_mode: str = "r2v"
    content_mode: str = "scripted"
    default_generation_mode: str = "r2v"


class UpdateSeriesRequest(CloudContentRequest):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    workflow_mode: str | None = None
    content_mode: str | None = None
    default_generation_mode: str | None = None
    art_direction: ArtDirection | None = None


class AddEpisodeRequest(CloudContentRequest):
    script_id: str
    episode_number: int | None = Field(default=None, gt=0)


class PromptConfigRequest(CloudContentRequest):
    storyboard_polish: str = ""
    video_polish: str = ""
    r2v_polish: str = ""
    entity_extraction: str = ""
    style_analysis: str = ""
    storyboard_extraction: str = ""


class AnalyzeStyleRequest(CloudContentRequest):
    script_text: str


class SaveArtDirectionRequest(CloudContentRequest):
    selected_style_id: str
    style_config: dict[str, Any]
    custom_styles: list[dict[str, Any]] = Field(default_factory=list)
    ai_recommendations: list[dict[str, Any]] = Field(default_factory=list)


PROMPT_DEFAULTS = {
    "storyboard_polish": DEFAULT_STORYBOARD_POLISH_PROMPT,
    "video_polish": DEFAULT_VIDEO_POLISH_PROMPT,
    "r2v_polish": DEFAULT_R2V_POLISH_PROMPT,
    "storyboard_extraction": DEFAULT_STORYBOARD_EXTRACTION_PROMPT,
}


def _content_response(stored: VersionedDocument[Any]) -> dict[str, Any]:
    payload = stored.document.model_dump(mode="json")
    payload["version"] = stored.version
    payload["schema_version"] = stored.schema_version
    return payload


def _prompt_config(payload: PromptConfigRequest, existing: PromptConfig) -> PromptConfig:
    return PromptConfig(
        **payload.model_dump(),
        polish_model=existing.polish_model,
    )


def _parse_expected_version(request: Request) -> int:
    raw_value = request.headers.get("if-match")
    if not raw_value:
        raise ContentVersionRequiredError("此操作需要 If-Match 内容版本")
    normalized = raw_value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    try:
        version = int(normalized)
    except ValueError as exc:
        raise ContentVersionRequiredError("If-Match 内容版本无效") from exc
    if version < 1:
        raise ContentVersionRequiredError("If-Match 内容版本无效")
    return version


def install_cloud_content_api(
    app: FastAPI,
    auth: AuthApplication,
    *,
    script_processor: ScriptProcessor | None = None,
    ai_submitter: AITaskSubmitter | None = None,
) -> CloudContentService:
    service = CloudContentService(
        auth.database,
        script_processor=script_processor,
    )
    router = APIRouter(tags=["工作区内容"])
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
                is_platform_admin=principal.is_platform_admin,
            ),
            workspace_id=canonical_workspace_id,
        )

    @router.post("/projects", status_code=201)
    def create_project(
        payload: CreateProjectRequest,
        request: Request,
        skip_analysis: bool = Query(default=False),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        idempotency_key = (
            require_idempotency_key(request, {})
            if ai_submitter is not None and not skip_analysis
            else None
        )
        stored = service.create_project(
            context,
            title=payload.title,
            text=payload.text,
            skip_analysis=(True if ai_submitter is not None else skip_analysis),
            workflow_mode=payload.workflow_mode,
            series_id=payload.series_id,
        )
        response = _content_response(stored)
        if ai_submitter is not None and idempotency_key is not None:
            response["ai_task"] = submit_ai_task(
                ai_submitter,
                context,
                capability="script.analysis",
                idempotency_key=idempotency_key,
                project_id=stored.document.id,
                content={
                    "operation": "project.create_and_analyze",
                    "title": payload.title,
                    "text": payload.text,
                    "workflow_mode": payload.workflow_mode,
                },
            )
        return response

    @router.get("/projects")
    @router.get("/projects/")
    def list_projects(
        context: WorkspaceContext = Depends(require_context),
    ) -> list[dict[str, Any]]:
        return [_content_response(item) for item in service.list_projects(context)]

    @router.get("/projects/{project_id}")
    def get_project(
        project_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(service.get_project(context, project_id))

    @router.put("/projects/{project_id}/text")
    def update_project_text(
        project_id: str,
        payload: UpdateProjectTextRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.update_project_text(
                context,
                project_id,
                payload.text,
                _parse_expected_version(request),
            )
        )

    @router.put("/projects/{project_id}/reparse")
    def reparse_project(
        project_id: str,
        payload: ReparseProjectRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        if ai_submitter is None:
            return _content_response(
                service.reparse_project(
                    context,
                    project_id,
                    payload.text,
                    _parse_expected_version(request),
                )
            )
        idempotency_key = require_idempotency_key(request, {})
        stored = service.update_project_text(
            context,
            project_id,
            payload.text,
            _parse_expected_version(request),
        )
        response = _content_response(stored)
        response["ai_task"] = submit_ai_task(
            ai_submitter,
            context,
            capability="script.analysis",
            idempotency_key=idempotency_key,
            project_id=project_id,
            content={
                "operation": "project.reparse",
                "text": payload.text,
            },
        )
        return response

    @router.post("/projects/{project_id}/toggle_starred")
    def toggle_project_starred(
        project_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.toggle_project_starred(
                context,
                project_id,
                _parse_expected_version(request),
            )
        )

    @router.delete("/projects/{project_id}")
    def delete_project(
        project_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        project = service.delete_project(
            context,
            project_id,
            _parse_expected_version(request),
        )
        return {
            "status": "deleted",
            "message": "项目已移入回收站，可在 30 天内恢复",
            "id": project.id,
            "title": project.title,
        }

    @router.get("/projects/{project_id}/prompt_config")
    def get_project_prompt_config(
        project_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        prompt_config, version = service.get_project_prompt_config(context, project_id)
        return {
            "prompt_config": prompt_config.model_dump(mode="json"),
            "defaults": PROMPT_DEFAULTS,
            "version": version,
        }

    @router.put("/projects/{project_id}/prompt_config")
    def update_project_prompt_config(
        project_id: str,
        payload: PromptConfigRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        existing, _version = service.get_project_prompt_config(context, project_id)
        stored = service.update_project_prompt_config(
            context,
            project_id,
            _prompt_config(payload, existing),
            _parse_expected_version(request),
        )
        return {
            "prompt_config": stored.document.prompt_config.model_dump(mode="json"),
            "version": stored.version,
        }

    @router.post("/projects/{project_id}/art_direction/analyze")
    def analyze_project_art_direction(
        project_id: str,
        payload: AnalyzeStyleRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        if ai_submitter is not None:
            return {
                "ai_task": submit_ai_task(
                    ai_submitter,
                    context,
                    capability="script.analysis",
                    idempotency_key=require_idempotency_key(request, {}),
                    project_id=project_id,
                    content={
                        "operation": "art_direction.analyze",
                        "script_text": payload.script_text,
                    },
                )
            }
        return {
            "recommendations": service.analyze_project_art_direction(
                context,
                project_id,
                payload.script_text,
            )
        }

    @router.post("/projects/{project_id}/art_direction/save")
    def save_project_art_direction(
        project_id: str,
        payload: SaveArtDirectionRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        art_direction = ArtDirection(
            selected_style_id=payload.selected_style_id,
            style_config=payload.style_config,
            custom_styles=payload.custom_styles,
            ai_recommendations=payload.ai_recommendations,
        )
        return _content_response(
            service.save_project_art_direction(
                context,
                project_id,
                art_direction,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/art_direction/clear")
    def clear_project_art_direction(
        project_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.clear_project_art_direction(
                context,
                project_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/series", status_code=201)
    def create_series(
        payload: CreateSeriesRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.create_series(
                context,
                title=payload.title,
                description=payload.description,
                workflow_mode=payload.workflow_mode,
                content_mode=payload.content_mode,
                default_generation_mode=payload.default_generation_mode,
            )
        )

    @router.get("/series")
    def list_series(
        context: WorkspaceContext = Depends(require_context),
    ) -> list[dict[str, Any]]:
        return [_content_response(item) for item in service.list_series(context)]

    @router.get("/series/{series_id}")
    def get_series(
        series_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        stored_series, episodes = service.list_series_episodes(context, series_id)
        payload = _content_response(stored_series)
        payload["episodes"] = [
            {
                "id": episode.document.id,
                "title": episode.document.title,
                "episode_number": episode.document.episode_number,
                "created_at": episode.document.created_at,
                "updated_at": episode.document.updated_at,
                "version": episode.version,
            }
            for episode in episodes
        ]
        return payload

    @router.put("/series/{series_id}")
    def update_series(
        series_id: str,
        payload: UpdateSeriesRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        updates = payload.model_dump(mode="json", exclude_unset=True)
        return _content_response(
            service.update_series(
                context,
                series_id,
                updates,
                _parse_expected_version(request),
            )
        )

    @router.delete("/series/{series_id}")
    def delete_series(
        series_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        series = service.delete_series(
            context,
            series_id,
            _parse_expected_version(request),
        )
        return {
            "status": "deleted",
            "message": "系列已移入回收站，可在 30 天内恢复",
            "id": series.id,
            "title": series.title,
        }

    @router.get("/series/{series_id}/episodes")
    def list_series_episodes(
        series_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> list[dict[str, Any]]:
        _series, episodes = service.list_series_episodes(context, series_id)
        return [_content_response(episode) for episode in episodes]

    @router.post("/series/{series_id}/episodes")
    def add_episode(
        series_id: str,
        payload: AddEpisodeRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.add_episode(
                context,
                series_id,
                payload.script_id,
                payload.episode_number,
                _parse_expected_version(request),
            )
        )

    @router.delete("/series/{series_id}/episodes/{project_id}")
    def remove_episode(
        series_id: str,
        project_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _content_response(
            service.remove_episode(
                context,
                series_id,
                project_id,
                _parse_expected_version(request),
            )
        )

    @router.get("/series/{series_id}/prompt_config")
    def get_series_prompt_config(
        series_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        prompt_config, version = service.get_series_prompt_config(context, series_id)
        return {
            "prompt_config": prompt_config.model_dump(mode="json"),
            "defaults": PROMPT_DEFAULTS,
            "version": version,
        }

    @router.put("/series/{series_id}/prompt_config")
    def update_series_prompt_config(
        series_id: str,
        payload: PromptConfigRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        existing, _version = service.get_series_prompt_config(context, series_id)
        stored = service.update_series_prompt_config(
            context,
            series_id,
            _prompt_config(payload, existing),
            _parse_expected_version(request),
        )
        return {
            "prompt_config": stored.document.prompt_config.model_dump(mode="json"),
            "version": stored.version,
        }

    if ai_adapter is not None:
        async def submit_script_operation(
            request: Request,
            context: WorkspaceContext = Depends(require_context),
        ) -> JSONResponse:
            operation = str(request.scope["route"].name).removeprefix("cloud_ai_")
            return await ai_adapter.submit_request(
                request,
                context,
                capability="script.analysis",
                operation=operation,
            )

        router.add_api_route(
            "/projects/{project_id}/extract_preview",
            submit_script_operation,
            methods=["POST"],
            name="cloud_ai_project.extract_preview",
        )
        router.add_api_route(
            "/projects/{project_id}/previous_episode/summary",
            submit_script_operation,
            methods=["POST"],
            name="cloud_ai_project.previous_episode.summary",
        )

    app.include_router(router)

    @app.exception_handler(ScopedDocumentNotFoundError)
    def handle_content_not_found(_request: Request, _exc: ScopedDocumentNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "CONTENT_NOT_FOUND", "message": "资源不存在"},
        )

    @app.exception_handler(OptimisticVersionConflictError)
    def handle_content_conflict(_request: Request, exc: OptimisticVersionConflictError):
        return JSONResponse(
            status_code=409,
            content={"code": "CONTENT_VERSION_CONFLICT", "message": str(exc)},
        )

    @app.exception_handler(ContentVersionRequiredError)
    def handle_version_required(_request: Request, exc: ContentVersionRequiredError):
        return JSONResponse(
            status_code=428,
            content={"code": "CONTENT_VERSION_REQUIRED", "message": str(exc)},
        )

    @app.exception_handler(InvalidRepositoryContextError)
    def handle_invalid_context(_request: Request, exc: InvalidRepositoryContextError):
        return JSONResponse(
            status_code=400,
            content={"code": "WORKSPACE_CONTEXT_INVALID", "message": str(exc)},
        )

    @app.exception_handler(DocumentPayloadValidationError)
    def handle_invalid_content(_request: Request, exc: DocumentPayloadValidationError):
        return JSONResponse(
            status_code=422,
            content={"code": "CONTENT_INVALID", "message": str(exc)},
        )

    app.state.cloud_content_service = service
    return service
