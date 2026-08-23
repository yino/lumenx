from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .contracts import WorkspaceContext
from .credentials import reject_plaintext_secrets
from .database import Database
from .db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    TicketHoldRecord,
    UsageEventRecord,
)
from .identifiers import parse_database_id


TaskStatus = Literal[
    "reserved",
    "queued",
    "running",
    "provider_succeeded",
    "succeeded",
    "failed",
    "cancelled",
    "support_review",
]
AttemptStatus = Literal[
    "pending",
    "running",
    "polling",
    "succeeded",
    "failed",
    "cancelled",
    "ambiguous",
]


TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "reserved": frozenset({"queued", "failed", "cancelled"}),
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"provider_succeeded", "failed", "cancelled", "support_review"}),
    "provider_succeeded": frozenset({"succeeded", "support_review"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "support_review": frozenset(),
}

ATTEMPT_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {"running", "succeeded", "failed", "cancelled", "ambiguous"}
    ),
    "running": frozenset(
        {"polling", "succeeded", "failed", "cancelled", "ambiguous"}
    ),
    "polling": frozenset({"succeeded", "failed", "cancelled", "ambiguous"}),
    "ambiguous": frozenset({"polling", "succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TERMINAL_TASK_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "support_review"}
)
TERMINAL_ATTEMPT_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


class AITaskStateError(RuntimeError):
    pass


class AITaskStateScopeNotFoundError(AITaskStateError):
    pass


class AITaskStateConflictError(AITaskStateError):
    pass


@dataclass(frozen=True, slots=True)
class AITaskStateSnapshot:
    id: str
    user_id: str
    workspace_id: str
    project_id: str | None
    capability: str
    status: str
    idempotency_key: str
    request_fingerprint: str
    request_payload: dict[str, Any]
    config_snapshot: dict[str, Any]
    tokens_per_ticket: int
    quoted_microtickets: int
    provider_billable: bool
    cancellation_requested_at: datetime | None
    support_review_reason: str | None
    safe_error_code: str | None
    safe_error_message: str | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AITaskAttemptSnapshot:
    id: str
    task_id: str
    retry_of_attempt_id: str | None
    retry_key: str | None
    attempt_number: int
    status: str
    config_snapshot: dict[str, Any]
    provider: str
    provider_model_id: str
    provider_request_id: str | None
    provider_task_id: str | None
    billable_acknowledged_at: datetime | None
    raw_usage: dict[str, Any] | None
    diagnostic: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AITaskBillingCorrelation:
    hold_ids: tuple[str, ...]
    open_hold_ids: tuple[str, ...]
    usage_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AITaskAggregate:
    task: AITaskStateSnapshot
    attempts: tuple[AITaskAttemptSnapshot, ...]
    billing: AITaskBillingCorrelation


@dataclass(frozen=True, slots=True)
class PaginatedAITasks:
    items: tuple[AITaskStateSnapshot, ...]
    total: int
    offset: int
    limit: int


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    try:
        copied = copy.deepcopy(dict(value))
        encoded = json.dumps(
            copied,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
        reject_plaintext_secrets(decoded, path=name)
    except (TypeError, ValueError) as exc:
        raise AITaskStateConflictError(str(exc) or f"{name}必须是有效的 JSON 对象") from exc
    if not isinstance(decoded, dict):
        raise AITaskStateConflictError(f"{name}必须是有效的 JSON 对象")
    return decoded


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_utc(value: datetime) -> datetime:
    normalized = _utc(value)
    assert normalized is not None
    return normalized


class AITaskStateMachine:
    @staticmethod
    def ensure_task_transition(current: str, target: str) -> None:
        if target not in TASK_TRANSITIONS.get(current, frozenset()):
            raise AITaskStateConflictError(
                f"AI 任务不能从 {current} 转换为 {target}"
            )

    @staticmethod
    def ensure_attempt_transition(current: str, target: str) -> None:
        if target not in ATTEMPT_TRANSITIONS.get(current, frozenset()):
            raise AITaskStateConflictError(
                f"AI 任务尝试不能从 {current} 转换为 {target}"
            )


class AITaskStateService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _id(value: str | int, name: str) -> int:
        try:
            return parse_database_id(value, field=name)
        except ValueError as exc:
            raise AITaskStateConflictError(f"{name}无效") from exc

    @staticmethod
    def _text(value: str, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AITaskStateConflictError(f"{name}无效")
        normalized = value.strip()
        if len(normalized) > maximum:
            raise AITaskStateConflictError(f"{name}过长")
        return normalized

    @staticmethod
    def _optional_text(value: str | None, name: str, maximum: int) -> str | None:
        if value is None:
            return None
        return AITaskStateService._text(value, name, maximum)

    @staticmethod
    def _task_snapshot(task: AITaskRecord) -> AITaskStateSnapshot:
        return AITaskStateSnapshot(
            id=str(task.id),
            user_id=str(task.user_id),
            workspace_id=str(task.workspace_id),
            project_id=str(task.project_id) if task.project_id is not None else None,
            capability=task.capability,
            status=task.status,
            idempotency_key=task.idempotency_key,
            request_fingerprint=task.request_fingerprint,
            request_payload=copy.deepcopy(dict(task.request_payload)),
            config_snapshot=copy.deepcopy(dict(task.config_snapshot)),
            tokens_per_ticket=task.tokens_per_ticket,
            quoted_microtickets=task.quoted_microtickets,
            provider_billable=task.provider_billable,
            cancellation_requested_at=_utc(task.cancellation_requested_at),
            support_review_reason=task.support_review_reason,
            safe_error_code=task.safe_error_code,
            safe_error_message=task.safe_error_message,
            result=copy.deepcopy(task.result) if task.result is not None else None,
            created_at=_required_utc(task.created_at),
            updated_at=_required_utc(task.updated_at),
            started_at=_utc(task.started_at),
            completed_at=_utc(task.completed_at),
        )

    @staticmethod
    def _attempt_snapshot(attempt: AITaskAttemptRecord) -> AITaskAttemptSnapshot:
        return AITaskAttemptSnapshot(
            id=str(attempt.id),
            task_id=str(attempt.task_id),
            retry_of_attempt_id=(
                str(attempt.retry_of_attempt_id)
                if attempt.retry_of_attempt_id is not None
                else None
            ),
            retry_key=attempt.retry_key,
            attempt_number=attempt.attempt_number,
            status=attempt.status,
            config_snapshot=copy.deepcopy(dict(attempt.config_snapshot)),
            provider=attempt.provider,
            provider_model_id=attempt.provider_model_id,
            provider_request_id=attempt.provider_request_id,
            provider_task_id=attempt.provider_task_id,
            billable_acknowledged_at=_utc(attempt.billable_acknowledged_at),
            raw_usage=(
                copy.deepcopy(attempt.raw_usage)
                if attempt.raw_usage is not None
                else None
            ),
            diagnostic=(
                copy.deepcopy(attempt.diagnostic)
                if attempt.diagnostic is not None
                else None
            ),
            created_at=_required_utc(attempt.created_at),
            started_at=_utc(attempt.started_at),
            completed_at=_utc(attempt.completed_at),
        )

    @classmethod
    def _context_ids(
        cls,
        context: WorkspaceContext,
        task_id: str,
    ) -> tuple[int, int, int]:
        return (
            cls._id(context.identity.user_id, "用户标识"),
            cls._id(context.workspace_id, "工作区标识"),
            cls._id(task_id, "任务标识"),
        )

    @staticmethod
    def _task_statement(
        *,
        user_id: int,
        workspace_id: int,
        task_id: int,
    ):
        return select(AITaskRecord).where(
            AITaskRecord.id == task_id,
            AITaskRecord.user_id == user_id,
            AITaskRecord.workspace_id == workspace_id,
        )

    @classmethod
    def _require_task(
        cls,
        session: Session,
        *,
        user_id: int,
        workspace_id: int,
        task_id: int,
        for_update: bool = False,
    ) -> AITaskRecord:
        statement = cls._task_statement(
            user_id=user_id,
            workspace_id=workspace_id,
            task_id=task_id,
        )
        task = session.scalar(statement.with_for_update() if for_update else statement)
        if task is None:
            raise AITaskStateScopeNotFoundError("AI 任务不存在")
        return task

    @staticmethod
    def _require_attempt(
        session: Session,
        task: AITaskRecord,
        attempt_id: int,
        *,
        for_update: bool = False,
    ) -> AITaskAttemptRecord:
        statement = select(AITaskAttemptRecord).where(
            AITaskAttemptRecord.id == attempt_id,
            AITaskAttemptRecord.user_id == task.user_id,
            AITaskAttemptRecord.workspace_id == task.workspace_id,
            AITaskAttemptRecord.task_id == task.id,
        )
        attempt = session.scalar(statement.with_for_update() if for_update else statement)
        if attempt is None:
            raise AITaskStateScopeNotFoundError("AI 任务尝试不存在")
        return attempt

    def get(self, context: WorkspaceContext, task_id: str) -> AITaskAggregate:
        user_id, workspace_id, canonical_task_id = self._context_ids(context, task_id)
        with self.database.transaction(context.identity) as session:
            task = self._require_task(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            attempts = tuple(
                self._attempt_snapshot(attempt)
                for attempt in session.scalars(
                    select(AITaskAttemptRecord)
                    .where(
                        AITaskAttemptRecord.user_id == user_id,
                        AITaskAttemptRecord.workspace_id == workspace_id,
                        AITaskAttemptRecord.task_id == canonical_task_id,
                    )
                    .order_by(AITaskAttemptRecord.attempt_number)
                )
            )
            holds = list(
                session.scalars(
                    select(TicketHoldRecord).where(
                        TicketHoldRecord.user_id == user_id,
                        TicketHoldRecord.workspace_id == workspace_id,
                        TicketHoldRecord.task_id == canonical_task_id,
                    )
                )
            )
            usage_ids = tuple(
                str(event_id)
                for event_id in session.scalars(
                    select(UsageEventRecord.id)
                    .where(
                        UsageEventRecord.user_id == user_id,
                        UsageEventRecord.workspace_id == workspace_id,
                        UsageEventRecord.task_id == canonical_task_id,
                    )
                    .order_by(UsageEventRecord.created_at, UsageEventRecord.id)
                )
            )
            return AITaskAggregate(
                task=self._task_snapshot(task),
                attempts=attempts,
                billing=AITaskBillingCorrelation(
                    hold_ids=tuple(str(hold.id) for hold in holds),
                    open_hold_ids=tuple(
                        str(hold.id) for hold in holds if hold.status == "held"
                    ),
                    usage_event_ids=usage_ids,
                ),
            )

    def list(
        self,
        context: WorkspaceContext,
        *,
        status: TaskStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> PaginatedAITasks:
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        if status is not None and status not in TASK_TRANSITIONS:
            raise AITaskStateConflictError("AI 任务状态筛选无效")
        if offset < 0 or limit < 1 or limit > 100:
            raise AITaskStateConflictError("AI 任务分页参数无效")

        filters = [
            AITaskRecord.user_id == user_id,
            AITaskRecord.workspace_id == workspace_id,
        ]
        if status is not None:
            filters.append(AITaskRecord.status == status)
        with self.database.transaction(context.identity) as session:
            total = int(
                session.scalar(
                    select(func.count()).select_from(AITaskRecord).where(*filters)
                )
                or 0
            )
            tasks = tuple(
                self._task_snapshot(task)
                for task in session.scalars(
                    select(AITaskRecord)
                    .where(*filters)
                    .order_by(AITaskRecord.created_at.desc(), AITaskRecord.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
        return PaginatedAITasks(
            items=tasks,
            total=total,
            offset=offset,
            limit=limit,
        )

    def create_initial_attempt(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        config_snapshot: Mapping[str, Any],
        provider: str,
        provider_model_id: str,
    ) -> AITaskAttemptSnapshot:
        user_id, workspace_id, canonical_task_id = self._context_ids(context, task_id)
        normalized_config = _json_object(config_snapshot, "任务尝试配置快照")
        normalized_provider = self._text(provider, "供应商", 80)
        normalized_model_id = self._text(provider_model_id, "供应商模型标识", 160)
        with self.database.transaction(context.identity) as session:
            task = self._require_task(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
                for_update=True,
            )
            if task.status in TERMINAL_TASK_STATUSES:
                raise AITaskStateConflictError("终态 AI 任务不能创建新尝试")
            existing = session.scalar(
                select(AITaskAttemptRecord).where(
                    AITaskAttemptRecord.task_id == task.id,
                    AITaskAttemptRecord.attempt_number == 1,
                )
            )
            if existing is not None:
                if (
                    existing.config_snapshot != normalized_config
                    or existing.provider != normalized_provider
                    or existing.provider_model_id != normalized_model_id
                ):
                    raise AITaskStateConflictError("首个任务尝试已使用不同配置创建")
                hold = session.scalar(
                    select(TicketHoldRecord).where(
                        TicketHoldRecord.task_id == task.id,
                        TicketHoldRecord.attempt_id.is_(None),
                    )
                )
                if hold is not None:
                    hold.attempt_id = existing.id
                return self._attempt_snapshot(existing)
            attempt = AITaskAttemptRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
                attempt_number=1,
                status="pending",
                config_snapshot=normalized_config,
                provider=normalized_provider,
                provider_model_id=normalized_model_id,
            )
            session.add(attempt)
            session.flush()
            hold = session.scalar(
                select(TicketHoldRecord).where(
                    TicketHoldRecord.task_id == task.id,
                    TicketHoldRecord.attempt_id.is_(None),
                )
            )
            if hold is None:
                raise AITaskStateConflictError("首个任务尝试缺少算力券预扣")
            hold.attempt_id = attempt.id
            session.flush()
            return self._attempt_snapshot(attempt)

    def transition_task(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        expected_statuses: Iterable[str],
        target_status: TaskStatus,
        result: Mapping[str, Any] | None = None,
        safe_error_code: str | None = None,
        safe_error_message: str | None = None,
        support_review_reason: str | None = None,
    ) -> AITaskStateSnapshot:
        user_id, workspace_id, canonical_task_id = self._context_ids(context, task_id)
        expected = frozenset(expected_statuses)
        if not expected:
            raise AITaskStateConflictError("必须提供期望任务状态")
        if target_status == "support_review":
            normalized_review_reason = self._text(
                support_review_reason or "",
                "人工复核原因",
                2000,
            )
        else:
            normalized_review_reason = None
        normalized_result = _json_object(result, "任务结果") if result is not None else None
        normalized_error_code = self._optional_text(
            safe_error_code,
            "安全错误码",
            80,
        )
        normalized_error_message = self._optional_text(
            safe_error_message,
            "安全错误信息",
            2000,
        )

        with self.database.transaction(context.identity) as session:
            task = self._require_task(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            if task.status not in expected:
                raise AITaskStateConflictError(
                    f"AI 任务当前状态为 {task.status}，不符合期望状态"
                )
            AITaskStateMachine.ensure_task_transition(task.status, target_status)
            now = datetime.now(timezone.utc)
            values: dict[str, Any] = {"status": target_status, "updated_at": now}
            if target_status == "running" and task.started_at is None:
                values["started_at"] = now
            if target_status in TERMINAL_TASK_STATUSES:
                values["completed_at"] = now
            if normalized_result is not None:
                values["result"] = normalized_result
            if normalized_error_code is not None:
                values["safe_error_code"] = normalized_error_code
            if normalized_error_message is not None:
                values["safe_error_message"] = normalized_error_message
            if normalized_review_reason is not None:
                values["support_review_reason"] = normalized_review_reason

            result_proxy = session.execute(
                update(AITaskRecord)
                .where(
                    AITaskRecord.id == canonical_task_id,
                    AITaskRecord.user_id == user_id,
                    AITaskRecord.workspace_id == workspace_id,
                    AITaskRecord.status == task.status,
                )
                .values(**values)
            )
            if result_proxy.rowcount != 1:
                raise AITaskStateConflictError("AI 任务状态已被其他执行者更新")
            session.expire(task)
            session.refresh(task)
            return self._task_snapshot(task)

    def transition_attempt(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        attempt_id: str,
        expected_statuses: Iterable[str],
        target_status: AttemptStatus,
        raw_usage: Mapping[str, Any] | None = None,
        diagnostic: Mapping[str, Any] | None = None,
    ) -> AITaskAttemptSnapshot:
        user_id, workspace_id, canonical_task_id = self._context_ids(context, task_id)
        canonical_attempt_id = self._id(attempt_id, "任务尝试标识")
        expected = frozenset(expected_statuses)
        if not expected:
            raise AITaskStateConflictError("必须提供期望尝试状态")
        normalized_usage = _json_object(raw_usage, "供应商用量") if raw_usage else None
        normalized_diagnostic = (
            _json_object(diagnostic, "任务尝试诊断") if diagnostic else None
        )

        with self.database.transaction(context.identity) as session:
            task = self._require_task(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            attempt = self._require_attempt(session, task, canonical_attempt_id)
            if attempt.status not in expected:
                raise AITaskStateConflictError(
                    f"AI 任务尝试当前状态为 {attempt.status}，不符合期望状态"
                )
            AITaskStateMachine.ensure_attempt_transition(attempt.status, target_status)
            now = datetime.now(timezone.utc)
            values: dict[str, Any] = {"status": target_status}
            if target_status == "running" and attempt.started_at is None:
                values["started_at"] = now
            if target_status in TERMINAL_ATTEMPT_STATUSES:
                values["completed_at"] = now
            if normalized_usage is not None:
                values["raw_usage"] = normalized_usage
            if normalized_diagnostic is not None:
                values["diagnostic"] = normalized_diagnostic

            result_proxy = session.execute(
                update(AITaskAttemptRecord)
                .where(
                    AITaskAttemptRecord.id == canonical_attempt_id,
                    AITaskAttemptRecord.user_id == user_id,
                    AITaskAttemptRecord.workspace_id == workspace_id,
                    AITaskAttemptRecord.task_id == canonical_task_id,
                    AITaskAttemptRecord.status == attempt.status,
                )
                .values(**values)
            )
            if result_proxy.rowcount != 1:
                raise AITaskStateConflictError("AI 任务尝试状态已被其他执行者更新")
            session.expire(attempt)
            session.refresh(attempt)
            return self._attempt_snapshot(attempt)

    def record_provider_submission(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        attempt_id: str,
        provider_request_id: str | None = None,
        provider_task_id: str | None = None,
        billable_acknowledged: bool = False,
    ) -> AITaskAttemptSnapshot:
        user_id, workspace_id, canonical_task_id = self._context_ids(context, task_id)
        canonical_attempt_id = self._id(attempt_id, "任务尝试标识")
        request_id = self._optional_text(
            provider_request_id,
            "供应商请求标识",
            255,
        )
        provider_id = self._optional_text(provider_task_id, "供应商任务标识", 255)
        if request_id is None and provider_id is None and not billable_acknowledged:
            raise AITaskStateConflictError("必须提供供应商请求或任务标识")

        with self.database.transaction(context.identity) as session:
            task = self._require_task(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
                for_update=True,
            )
            attempt = self._require_attempt(
                session,
                task,
                canonical_attempt_id,
                for_update=True,
            )
            if attempt.status not in {"running", "polling", "ambiguous"}:
                raise AITaskStateConflictError("AI 任务尝试当前状态不能记录供应商提交")
            if request_id is not None and attempt.provider_request_id not in {
                None,
                request_id,
            }:
                raise AITaskStateConflictError("供应商请求标识与已记录值冲突")
            if provider_id is not None and attempt.provider_task_id not in {
                None,
                provider_id,
            }:
                raise AITaskStateConflictError("供应商任务标识与已记录值冲突")
            if request_id is not None:
                attempt.provider_request_id = request_id
            if provider_id is not None:
                attempt.provider_task_id = provider_id
            if billable_acknowledged and attempt.billable_acknowledged_at is None:
                attempt.billable_acknowledged_at = datetime.now(timezone.utc)
            if billable_acknowledged:
                task.provider_billable = True
            session.flush()
            return self._attempt_snapshot(attempt)

    def record_attempt_usage(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        attempt_id: str,
        raw_usage: Mapping[str, Any],
    ) -> AITaskAttemptSnapshot:
        user_id, workspace_id, canonical_task_id = self._context_ids(context, task_id)
        canonical_attempt_id = self._id(attempt_id, "任务尝试标识")
        normalized_usage = _json_object(raw_usage, "供应商用量")
        with self.database.transaction(context.identity) as session:
            task = self._require_task(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
                for_update=True,
            )
            attempt = self._require_attempt(
                session,
                task,
                canonical_attempt_id,
                for_update=True,
            )
            if attempt.status not in {"running", "polling", "ambiguous"}:
                raise AITaskStateConflictError("AI 任务尝试当前状态不能记录供应商用量")
            if attempt.raw_usage is not None and attempt.raw_usage != normalized_usage:
                raise AITaskStateConflictError("供应商用量与已记录值冲突")
            attempt.raw_usage = normalized_usage
            session.flush()
            return self._attempt_snapshot(attempt)

    def request_cancellation(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
    ) -> AITaskStateSnapshot:
        user_id, workspace_id, canonical_task_id = self._context_ids(context, task_id)
        with self.database.transaction(context.identity) as session:
            task = self._require_task(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
                for_update=True,
            )
            if task.status in TERMINAL_TASK_STATUSES:
                raise AITaskStateConflictError("终态 AI 任务不能请求取消")
            if task.cancellation_requested_at is None:
                task.cancellation_requested_at = datetime.now(timezone.utc)
                session.flush()
            return self._task_snapshot(task)
