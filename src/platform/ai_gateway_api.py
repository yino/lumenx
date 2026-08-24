from __future__ import annotations

import copy
import json
import logging
from collections.abc import Mapping
from typing import Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .ai_gateway import (
    AIGateway,
    AIGatewayConfigurationError,
    AIGatewayDispatchError,
    AIGatewayResourceNotFoundError,
    SubmittedAITask,
)
from .ai_gateway_contract import AIGatewayContractError
from .ai_request_policy import ClientAIOverrideError, enforce_no_client_ai_overrides
from .ai_task_state import AITaskStateService
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .configuration_service import ConfigurationService
from .contracts import TaskDispatcher, UserContext, WorkspaceContext
from .feature_flags import CloudFeatureDisabledError, CloudFeatureGate
from .identifiers import parse_database_id
from .model_routing import (
    DatabaseModelConfigurationProvider,
    ModelRouteUnavailableError,
)
from .ticket_math import MICROTICKETS_PER_TICKET
from .ticket_reservation import (
    AIConcurrencyLimitError,
    InsufficientTicketBalanceError,
    TicketReservationConflictError,
    TicketReservationScopeNotFoundError,
    TicketReservationService,
)


logger = logging.getLogger(__name__)


class AITaskSubmitter(Protocol):
    def submit(
        self,
        context: WorkspaceContext,
        payload: Mapping[str, Any],
    ) -> SubmittedAITask: ...


class FeatureGatedAITaskSubmitter:
    def __init__(
        self,
        submitter: AITaskSubmitter,
        feature_gate: CloudFeatureGate,
    ) -> None:
        self.submitter = submitter
        self.feature_gate = feature_gate

    def submit(
        self,
        context: WorkspaceContext,
        payload: Mapping[str, Any],
    ) -> SubmittedAITask:
        self.feature_gate.require_new_ai_task()
        return self.submitter.submit(context, payload)


class CloudAIGatewayFacade:
    def __init__(self, database, dispatcher: TaskDispatcher) -> None:
        self.database = database
        self.dispatcher = dispatcher
        self.configuration = ConfigurationService(database)
        self.reservation = TicketReservationService(database)
        self.task_state = AITaskStateService(database)

    def submit(
        self,
        context: WorkspaceContext,
        payload: Mapping[str, Any],
    ) -> SubmittedAITask:
        routing = DatabaseModelConfigurationProvider(
            self.configuration,
            context.identity,
        )
        return AIGateway(
            reservation=self.reservation,
            task_state=self.task_state,
            model_configuration=routing,
            dispatcher=self.dispatcher,
        ).submit(context, payload)


def _ticket_text(microtickets: int) -> str:
    whole, fraction = divmod(abs(microtickets), MICROTICKETS_PER_TICKET)
    sign = "-" if microtickets < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def submitted_task_response(task: SubmittedAITask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "attempt_id": task.attempt_id,
        "status": task.status,
        "capability": task.capability,
        "quoted_microtickets": str(task.quoted_microtickets),
        "quoted_tickets": _ticket_text(task.quoted_microtickets),
        "tokens_per_ticket": str(task.tokens_per_ticket),
        "actual_model": {
            "display_name": task.model_display_name,
            "model_id": task.provider_model_id,
        },
        "reused": task.reused,
        "dispatched": task.dispatched,
    }


def effective_engine_response(route) -> dict[str, Any]:
    is_gpt_image_2 = (
        route.capability == "image.t2i"
        and route.provider_model_id == "gpt-image-2"
    )
    provider_names = {"xlinks": "Xlinks", "mulerouter": "MuleRouter"}
    return {
        "capability": route.capability,
        "model_id": route.provider_model_id,
        "model_display_name": route.display_name,
        "provider_display_name": provider_names.get(
            route.provider,
            route.provider.replace("_", " ").title(),
        ),
        "features": {
            "character_design_sheet": is_gpt_image_2,
        },
    }


def require_idempotency_key(request: Request, payload: Mapping[str, Any]) -> str:
    header_value = request.headers.get("idempotency-key", "").strip()
    body_value = payload.get("idempotency_key")
    normalized_body = str(body_value).strip() if body_value is not None else ""
    if header_value and normalized_body and header_value != normalized_body:
        raise AIGatewayContractError(
            "AI_IDEMPOTENCY_CONFLICT",
            "请求头与请求体的幂等键不一致",
        )
    value = header_value or normalized_body
    if not value:
        raise AIGatewayContractError(
            "AI_IDEMPOTENCY_REQUIRED",
            "AI 请求必须提供幂等键",
        )
    return value


def submit_ai_task(
    submitter: AITaskSubmitter,
    context: WorkspaceContext,
    *,
    capability: str,
    idempotency_key: str,
    content: str | Mapping[str, Any],
    project_id: str | None = None,
    resource_ids: Mapping[str, list[str]] | None = None,
    media_ids: list[str] | None = None,
    parameters: Mapping[str, str | int | float | bool] | None = None,
) -> dict[str, Any]:
    task = submitter.submit(
        context,
        {
            "capability": capability,
            "idempotency_key": idempotency_key,
            "project_id": project_id,
            "resource_ids": copy.deepcopy(dict(resource_ids or {})),
            "media_ids": list(media_ids or []),
            "content": (
                copy.deepcopy(dict(content))
                if isinstance(content, Mapping)
                else content
            ),
            "parameters": copy.deepcopy(dict(parameters or {})),
        },
    )
    return submitted_task_response(task)


def _media_ids(value: Any, *, path: str = "请求") -> list[str]:
    identifiers: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if key == "media_ids":
                if not isinstance(item, list):
                    raise AIGatewayContractError(
                        "AI_REQUEST_INVALID",
                        "媒体标识列表格式无效",
                    )
                candidates = item
            elif key == "media_id" or key.endswith("_media_id"):
                candidates = [item] if item is not None else []
            else:
                identifiers.extend(_media_ids(item, path=f"{path}.{raw_key}"))
                continue
            for candidate in candidates:
                try:
                    identifiers.append(
                        str(parse_database_id(candidate, field="媒体 ID"))
                    )
                except ValueError as exc:
                    raise AIGatewayContractError(
                        "AI_REQUEST_INVALID",
                        "媒体标识无效",
                    ) from exc
    elif isinstance(value, list):
        for index, item in enumerate(value):
            identifiers.extend(_media_ids(item, path=f"{path}[{index}]"))
    return identifiers


def _path_resource_ids(path_params: Mapping[str, Any]) -> dict[str, list[str]]:
    field_mapping = {
        "series_id": "series",
        "asset_id": "asset",
        "frame_id": "storyboard_frame",
        "voice_id": "voice",
    }
    resources: dict[str, list[str]] = {}
    for field, kind in field_mapping.items():
        value = path_params.get(field)
        if value is None:
            continue
        try:
            resources[kind] = [
                str(parse_database_id(value, field=f"{kind} ID"))
            ]
        except ValueError:
            continue
    return resources


class CloudAIRequestAdapter:
    def __init__(self, submitter: AITaskSubmitter) -> None:
        self.submitter = submitter

    async def submit_request(
        self,
        request: Request,
        context: WorkspaceContext,
        *,
        capability: str,
        operation: str,
    ) -> JSONResponse:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"", "application/json"}:
            raise AIGatewayContractError(
                "AI_MEDIA_ID_REQUIRED",
                "请先上传媒体，再使用媒体 ID 发起 AI 请求",
            )
        try:
            raw_payload = await request.json() if content_type else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AIGatewayContractError(
                "AI_REQUEST_INVALID",
                "AI 请求格式无效",
            ) from exc
        if not isinstance(raw_payload, dict):
            raise AIGatewayContractError(
                "AI_REQUEST_INVALID",
                "AI 请求格式无效",
            )
        effective_capability = capability
        if (
            operation == "video.task.create"
            and raw_payload.get("generation_mode") == "r2v"
        ):
            effective_capability = "video.r2v"
        try:
            enforce_no_client_ai_overrides(raw_payload)
        except ClientAIOverrideError as exc:
            raise AIGatewayContractError(
                "AI_OVERRIDE_FORBIDDEN",
                str(exc),
                paths=exc.paths,
            ) from exc

        idempotency_key = require_idempotency_key(request, raw_payload)
        payload = copy.deepcopy(raw_payload)
        payload.pop("idempotency_key", None)
        explicit_content = payload.pop("content", None)
        explicit_parameters = payload.pop("parameters", {})
        explicit_resources = payload.pop("resource_ids", {})
        payload.pop("media_ids", None)
        if not isinstance(explicit_parameters, dict):
            raise AIGatewayContractError("AI_REQUEST_INVALID", "AI 创作参数格式无效")
        if not isinstance(explicit_resources, dict):
            raise AIGatewayContractError("AI_REQUEST_INVALID", "AI 资源标识格式无效")

        resources = _path_resource_ids(request.path_params)
        for kind, identifiers in explicit_resources.items():
            if not isinstance(identifiers, list):
                raise AIGatewayContractError("AI_REQUEST_INVALID", "AI 资源标识格式无效")
            resources.setdefault(str(kind), []).extend(str(item) for item in identifiers)
        media_ids = list(dict.fromkeys(_media_ids(raw_payload)))
        content: str | Mapping[str, Any]
        if explicit_content is not None:
            content = explicit_content
        else:
            content = {"operation": operation, **payload}
        project_id = request.path_params.get("project_id")
        if project_id is None and raw_payload.get("script_id") is not None:
            try:
                project_id = str(
                    parse_database_id(raw_payload["script_id"], field="项目 ID")
                )
            except ValueError as exc:
                raise AIGatewayContractError(
                    "AI_REQUEST_INVALID",
                    "项目 ID 无效",
                ) from exc
        response = submit_ai_task(
            self.submitter,
            context,
            capability=effective_capability,
            idempotency_key=idempotency_key,
            content=content,
            project_id=str(project_id) if project_id is not None else None,
            resource_ids=resources,
            media_ids=media_ids,
            parameters=explicit_parameters,
        )
        return JSONResponse(status_code=202, content=response)


def install_cloud_ai_gateway_api(
    app: FastAPI,
    auth: AuthApplication,
    *,
    dispatcher: TaskDispatcher | None = None,
    submitter: AITaskSubmitter | None = None,
) -> AITaskSubmitter:
    if submitter is None:
        if dispatcher is None:
            raise ValueError("云端 AI 网关必须配置持久任务分发器")
        submitter = CloudAIGatewayFacade(auth.database, dispatcher)
    feature_gate = getattr(auth, "feature_gate", None)
    if feature_gate is not None:
        submitter = FeatureGatedAITaskSubmitter(submitter, feature_gate)
    router = APIRouter(prefix="/ai", tags=["AI 网关"])

    def require_context(request: Request) -> WorkspaceContext:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf_token = (
            request.headers.get("x-csrf-token")
            if request.method in UNSAFE_METHODS
            else None
        )
        principal: SessionPrincipal = auth.sessions.resolve(
            token,
            csrf_token=csrf_token,
        )
        raw_workspace_id = request.headers.get("x-workspace-id", "").strip()
        try:
            workspace_id = str(parse_database_id(raw_workspace_id, field="工作区 ID"))
        except ValueError as exc:
            raise AIGatewayContractError(
                "AI_CONTEXT_INVALID",
                "请选择有效的工作区",
            ) from exc
        return WorkspaceContext(
            identity=UserContext(
                user_id=str(principal.user_id),
                session_id=str(principal.session_id),
            ),
            workspace_id=workspace_id,
        )

    @router.post("/generate", status_code=202)
    def submit_generation(
        payload: dict[str, Any],
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return submitted_task_response(submitter.submit(context, payload))

    @router.get("/effective-engine")
    def get_effective_engine(
        capability: str = "image.t2i",
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        if capability != "image.t2i":
            raise AIGatewayContractError(
                "AI_REQUEST_INVALID",
                "当前仅支持查询文生图有效引擎",
            )
        routing = DatabaseModelConfigurationProvider(
            ConfigurationService(auth.database),
            context.identity,
        )
        try:
            route = routing.select_route(capability, {})
        except ModelRouteUnavailableError as exc:
            raise AIGatewayConfigurationError(str(exc)) from exc
        return effective_engine_response(route)

    app.include_router(router)
    app.state.cloud_ai_gateway = submitter

    @app.exception_handler(AIGatewayContractError)
    def handle_contract_error(_request: Request, exc: AIGatewayContractError):
        status_code = 400 if exc.code == "AI_CONTEXT_INVALID" else 422
        return JSONResponse(
            status_code=status_code,
            content={"code": exc.code, "message": str(exc)},
        )

    @app.exception_handler(AIGatewayResourceNotFoundError)
    @app.exception_handler(TicketReservationScopeNotFoundError)
    def handle_not_found(_request: Request, _exc: Exception):
        return JSONResponse(
            status_code=404,
            content={"code": "AI_RESOURCE_NOT_FOUND", "message": "AI 请求资源不存在"},
        )

    @app.exception_handler(InsufficientTicketBalanceError)
    def handle_insufficient_balance(
        _request: Request,
        exc: InsufficientTicketBalanceError,
    ):
        return JSONResponse(
            status_code=402,
            content={
                "code": "TICKET_BALANCE_INSUFFICIENT",
                "message": "可用算力券不足",
                "available_microtickets": str(exc.available_microtickets),
                "required_microtickets": str(exc.required_microtickets),
            },
        )

    @app.exception_handler(AIConcurrencyLimitError)
    def handle_concurrency_limit(_request: Request, exc: AIConcurrencyLimitError):
        return JSONResponse(
            status_code=429,
            content={
                "code": "AI_CONCURRENCY_LIMITED",
                "message": str(exc),
                "maximum_concurrency": exc.maximum_concurrency,
            },
        )

    @app.exception_handler(AIGatewayConfigurationError)
    def handle_configuration_error(request: Request, exc: AIGatewayConfigurationError):
        logger.warning(
            "AI gateway configuration unavailable path=%s code=%s message=%s",
            request.url.path,
            "AI_CONFIGURATION_UNAVAILABLE",
            str(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"code": "AI_CONFIGURATION_UNAVAILABLE", "message": str(exc)},
        )

    @app.exception_handler(AIGatewayDispatchError)
    def handle_dispatch_error(_request: Request, exc: AIGatewayDispatchError):
        return JSONResponse(
            status_code=202,
            content={
                "code": "AI_TASK_RECOVERY_PENDING",
                "message": str(exc),
                "task_id": exc.task_id,
            },
        )

    @app.exception_handler(TicketReservationConflictError)
    def handle_reservation_conflict(
        _request: Request,
        exc: TicketReservationConflictError,
    ):
        return JSONResponse(
            status_code=409,
            content={"code": "AI_RESERVATION_CONFLICT", "message": str(exc)},
        )

    return submitter
