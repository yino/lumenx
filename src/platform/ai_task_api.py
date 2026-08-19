from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .ai_task_state import (
    AITaskAggregate,
    AITaskStateConflictError,
    AITaskStateScopeNotFoundError,
    AITaskStateService,
    AITaskStateSnapshot,
    PaginatedAITasks,
    TERMINAL_TASK_STATUSES,
    TaskStatus,
)
from .audit import AuditService
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .contracts import UserContext, WorkspaceContext
from .identifiers import parse_database_id
from .ticket_math import MICROTICKETS_PER_TICKET
from .ticket_settlement import (
    FailureSettlement,
    TicketCancellationPendingError,
    TicketSettlementConflictError,
    TicketSettlementScopeNotFoundError,
    TicketSettlementService,
)


TASK_STATUS_ZH = {
    "reserved": "预扣中",
    "queued": "排队中",
    "running": "生成中",
    "provider_succeeded": "结果处理中",
    "succeeded": "已完成",
    "failed": "已失败",
    "cancelled": "已取消",
    "support_review": "计费待复核",
}
RESULT_CONTENT_CAPABILITIES = frozenset(
    {"script.analysis", "prompt.polish", "speech.tts"}
)
MAX_PROJECTED_TEXT_RESULT_BYTES = 2 * 1024 * 1024


class AITaskAPIContextError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AITaskCancellationResult:
    task: AITaskStateSnapshot
    outcome: str
    message: str
    released_microtickets: int = 0


def _ticket_text(microtickets: int) -> str:
    whole, fraction = divmod(abs(microtickets), MICROTICKETS_PER_TICKET)
    sign = "-" if microtickets < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _model_projection(
    task: AITaskStateSnapshot,
    aggregate: AITaskAggregate | None = None,
) -> dict[str, str]:
    snapshot = task.config_snapshot
    display_name = str(snapshot.get("display_name") or "平台模型")
    model_id = str(snapshot.get("provider_model_id") or "")
    if aggregate is not None and aggregate.attempts:
        latest = aggregate.attempts[-1]
        display_name = str(latest.config_snapshot.get("display_name") or display_name)
        model_id = latest.provider_model_id or model_id
    return {"display_name": display_name, "model_id": model_id}


def _result_media_ids(result: dict[str, Any] | None) -> list[str]:
    if not result:
        return []
    values = result.get("media_ids")
    if not isinstance(values, list):
        return []
    media_ids: list[str] = []
    for value in values:
        try:
            media_ids.append(str(parse_database_id(value, field="媒体 ID")))
        except ValueError:
            continue
    return media_ids


def _result_content(task: AITaskStateSnapshot) -> str | dict[str, Any] | None:
    if task.capability not in RESULT_CONTENT_CAPABILITIES or not task.result:
        return None
    content = task.result.get("content")
    if not isinstance(content, (str, Mapping)):
        return None
    projected = dict(content) if isinstance(content, Mapping) else content
    try:
        encoded = json.dumps(projected, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > MAX_PROJECTED_TEXT_RESULT_BYTES:
        return None
    return projected


def _task_projection(
    task: AITaskStateSnapshot,
    *,
    include_result_content: bool = False,
) -> dict[str, Any]:
    response = {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "project_id": task.project_id,
        "capability": task.capability,
        "status": task.status,
        "status_zh": TASK_STATUS_ZH[task.status],
        "quoted_microtickets": str(task.quoted_microtickets),
        "quoted_tickets": _ticket_text(task.quoted_microtickets),
        "cancellation_requested": task.cancellation_requested_at is not None,
        "support_review": task.status == "support_review",
        "safe_error": (
            {
                "code": task.safe_error_code,
                "message": task.safe_error_message,
            }
            if task.safe_error_code or task.safe_error_message
            else None
        ),
        "media_ids": _result_media_ids(task.result),
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "started_at": _iso(task.started_at),
        "completed_at": _iso(task.completed_at),
    }
    if include_result_content:
        response["result_content"] = _result_content(task)
    return response


def _page_projection(page: PaginatedAITasks) -> dict[str, Any]:
    return {
        "items": [_task_projection(task) for task in page.items],
        "total": page.total,
        "offset": page.offset,
        "limit": page.limit,
    }


def _detail_projection(aggregate: AITaskAggregate) -> dict[str, Any]:
    response = _task_projection(aggregate.task, include_result_content=True)
    response.update(
        {
            "actual_model": _model_projection(aggregate.task, aggregate),
            "tokens_per_ticket": str(aggregate.task.tokens_per_ticket),
            "attempts": [
                {
                    "id": attempt.id,
                    "attempt_number": attempt.attempt_number,
                    "status": attempt.status,
                    "model_id": attempt.provider_model_id,
                    "created_at": _iso(attempt.created_at),
                    "started_at": _iso(attempt.started_at),
                    "completed_at": _iso(attempt.completed_at),
                }
                for attempt in aggregate.attempts
            ],
            "billing": {
                "hold_ids": list(aggregate.billing.hold_ids),
                "open_hold_ids": list(aggregate.billing.open_hold_ids),
                "usage_event_ids": list(aggregate.billing.usage_event_ids),
            },
        }
    )
    return response


class AITaskCancellationService:
    def __init__(self, task_state: AITaskStateService) -> None:
        self.task_state = task_state
        self.settlement = TicketSettlementService(task_state.database)

    def _request_or_resolve_terminal(
        self,
        context: WorkspaceContext,
        task_id: str,
    ) -> AITaskCancellationResult:
        current = self.task_state.get(context, task_id).task
        if current.status == "cancelled":
            return AITaskCancellationResult(
                task=current,
                outcome="cancelled",
                message="任务已取消",
            )
        if current.status in TERMINAL_TASK_STATUSES:
            raise AITaskStateConflictError("当前任务状态不能取消")
        requested = self.task_state.request_cancellation(context, task_id=task_id)
        return AITaskCancellationResult(
            task=requested,
            outcome="requested",
            message="已提交取消请求，等待供应商确认计费状态",
        )

    def cancel(
        self,
        context: WorkspaceContext,
        task_id: str,
    ) -> AITaskCancellationResult:
        aggregate = self.task_state.get(context, task_id)
        if aggregate.task.status == "cancelled":
            return AITaskCancellationResult(
                task=aggregate.task,
                outcome="cancelled",
                message="任务已取消",
            )
        if aggregate.task.status in TERMINAL_TASK_STATUSES:
            raise AITaskStateConflictError("当前任务状态不能取消")
        if aggregate.task.status == "provider_succeeded":
            return self._request_or_resolve_terminal(context, task_id)

        attempt_id = aggregate.attempts[-1].id if aggregate.attempts else None
        try:
            settlement: FailureSettlement = self.settlement.release_nonbillable(
                context,
                task_id=task_id,
                attempt_id=attempt_id,
                outcome="cancelled",
                reason="用户取消 AI 任务",
                safe_error_code="AI_TASK_CANCELLED",
                safe_error_message="任务已取消，预扣算力券已释放",
            )
        except TicketCancellationPendingError:
            return self._request_or_resolve_terminal(context, task_id)
        except TicketSettlementConflictError:
            current = self.task_state.get(context, task_id).task
            if current.status not in TERMINAL_TASK_STATUSES:
                return self._request_or_resolve_terminal(context, task_id)
            if current.status == "cancelled":
                return AITaskCancellationResult(
                    task=current,
                    outcome="cancelled",
                    message="任务已取消",
                )
            raise
        task = self.task_state.get(context, task_id).task
        return AITaskCancellationResult(
            task=task,
            outcome="cancelled",
            message="任务已取消，预扣算力券已释放",
            released_microtickets=settlement.released_microtickets,
        )


def install_cloud_ai_task_api(
    app: FastAPI,
    auth: AuthApplication,
) -> AITaskStateService:
    service = AITaskStateService(auth.database)
    cancellation = AITaskCancellationService(service)
    audit = AuditService(auth.database)
    router = APIRouter(prefix="/ai/tasks", tags=["AI 任务"])

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
            raise AITaskAPIContextError("请选择有效的工作区") from exc
        return WorkspaceContext(
            identity=UserContext(
                user_id=str(principal.user_id),
                session_id=str(principal.session_id),
            ),
            workspace_id=workspace_id,
        )

    @router.get("")
    def list_tasks(
        status: TaskStatus | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _page_projection(
            service.list(context, status=status, offset=offset, limit=limit)
        )

    @router.get("/{task_id}/status")
    def get_task_status(
        task_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _task_projection(
            service.get(context, task_id).task,
            include_result_content=True,
        )

    @router.get("/{task_id}")
    def get_task_detail(
        task_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _detail_projection(service.get(context, task_id))

    @router.post("/{task_id}/cancel")
    def cancel_task(
        task_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        result = cancellation.cancel(context, task_id)
        audit.record(
            context.identity,
            action="ai_task.cancel",
            target_type="ai_task",
            target_id=task_id,
            request=request,
            workspace_id=context.workspace_id,
            after={
                "status": result.task.status,
                "outcome": result.outcome,
                "released_microtickets": result.released_microtickets,
            },
        )
        return {
            **_task_projection(result.task),
            "cancellation_outcome": result.outcome,
            "message": result.message,
            "released_microtickets": str(result.released_microtickets),
            "released_tickets": _ticket_text(result.released_microtickets),
        }

    app.include_router(router)
    app.state.ai_task_state_service = service
    app.state.ai_task_cancellation_service = cancellation

    @app.exception_handler(AITaskAPIContextError)
    def handle_context_error(_request: Request, exc: AITaskAPIContextError):
        return JSONResponse(
            status_code=400,
            content={"code": "WORKSPACE_CONTEXT_INVALID", "message": str(exc)},
        )

    @app.exception_handler(AITaskStateScopeNotFoundError)
    @app.exception_handler(TicketSettlementScopeNotFoundError)
    def handle_not_found(_request: Request, _exc: Exception):
        return JSONResponse(
            status_code=404,
            content={"code": "AI_TASK_NOT_FOUND", "message": "AI 任务不存在"},
        )

    @app.exception_handler(AITaskStateConflictError)
    @app.exception_handler(TicketSettlementConflictError)
    def handle_conflict(_request: Request, exc: Exception):
        return JSONResponse(
            status_code=409,
            content={"code": "AI_TASK_CONFLICT", "message": str(exc)},
        )

    return service
