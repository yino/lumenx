from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Protocol

from sqlalchemy import and_, or_, select

from .ai_task_state import AITaskStateMachine, AITaskStateService
from .ai_worker import (
    WORKER_IDENTITY,
    WorkerModelClientFactory,
    WorkerOutputFinalizer,
    WorkerTaskLease,
    model_route_from_snapshot,
    normalize_provider_usage,
)
from .contracts import UserContext, WorkspaceContext
from .database import Database
from .db_models import AITaskAttemptRecord, AITaskRecord
from .model_routing import RequestScopedModelClient
from .identifiers import parse_database_id
from .ticket_settlement import TicketSettlementService


class ProviderRecoveryInvoker(Protocol):
    def resume(
        self,
        client: RequestScopedModelClient,
        task: "ProviderRecoveryLease",
    ) -> "ProviderRecoveryOutcome": ...


class UnsupportedProviderRecoveryInvoker:
    def resume(
        self,
        client: RequestScopedModelClient,
        task: "ProviderRecoveryLease",
    ) -> "ProviderRecoveryOutcome":
        raise RuntimeError("当前供应商不支持异步任务恢复")


@dataclass(frozen=True, slots=True)
class ProviderRecoveryOutcome:
    status: Literal["pending", "succeeded"]
    raw_usage: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderRecoveryLease:
    task: WorkerTaskLease
    provider_task_id: str
    provider_request_id: str | None


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    task_id: str
    status: str
    lease: ProviderRecoveryLease | None = None
    context: WorkspaceContext | None = None
    attempt_id: str | None = None
    provider_billable: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryExecutionResult:
    task_id: str
    status: str
    recovered: bool
    attempt_id: str | None = None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class AIRecoveryRepository:
    def __init__(
        self,
        database: Database,
        *,
        worker_identity: UserContext = WORKER_IDENTITY,
        stale_after_seconds: int = 300,
        running_stale_after_seconds: int = 900,
    ) -> None:
        if stale_after_seconds < 0:
            raise ValueError("恢复租约过期时间不能为负数")
        if running_stale_after_seconds < stale_after_seconds:
            raise ValueError("运行中任务的恢复等待时间不能短于普通恢复等待时间")
        self.database = database
        self.worker_identity = worker_identity
        self.stale_after = timedelta(seconds=stale_after_seconds)
        self.running_stale_after = timedelta(seconds=running_stale_after_seconds)

    @staticmethod
    def _task_id(value: str) -> int:
        try:
            return parse_database_id(value, field="任务 ID")
        except ValueError as exc:
            raise ValueError("AI 任务标识无效") from exc

    def list_candidate_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("恢复任务数量必须为正整数")
        now = datetime.now(timezone.utc)
        stale_before = now - self.stale_after
        running_stale_before = now - self.running_stale_after
        with self.database.transaction(self.worker_identity) as session:
            identifiers = session.scalars(
                select(AITaskRecord.id)
                .join(
                    AITaskAttemptRecord,
                    AITaskAttemptRecord.task_id == AITaskRecord.id,
                )
                .where(
                    AITaskRecord.status == "running",
                    AITaskAttemptRecord.status.in_({"running", "polling", "ambiguous"}),
                    or_(
                        and_(
                            AITaskAttemptRecord.status == "running",
                            AITaskRecord.updated_at <= running_stale_before,
                        ),
                        and_(
                            AITaskAttemptRecord.status.in_({"polling", "ambiguous"}),
                            AITaskRecord.updated_at <= stale_before,
                        ),
                    ),
                )
                .group_by(AITaskRecord.id, AITaskRecord.updated_at)
                .order_by(AITaskRecord.updated_at, AITaskRecord.id)
                .limit(limit)
            )
            return tuple(str(identifier) for identifier in identifiers)

    def claim(self, task_id: str) -> RecoveryClaim:
        canonical_task_id = self._task_id(task_id)
        with self.database.transaction(self.worker_identity) as session:
            task = session.scalar(
                select(AITaskRecord)
                .where(AITaskRecord.id == canonical_task_id)
                .with_for_update()
            )
            if task is None:
                return RecoveryClaim(task_id=str(canonical_task_id), status="missing")
            if task.status != "running":
                return RecoveryClaim(task_id=str(task.id), status=task.status)
            attempt = session.scalar(
                select(AITaskAttemptRecord)
                .where(
                    AITaskAttemptRecord.task_id == task.id,
                    AITaskAttemptRecord.status.in_(
                        {"running", "polling", "ambiguous"}
                    ),
                )
                .order_by(AITaskAttemptRecord.attempt_number.desc())
                .with_for_update()
            )
            if attempt is None:
                return RecoveryClaim(task_id=str(task.id), status="missing_attempt")

            context = WorkspaceContext(
                identity=UserContext(user_id=str(task.user_id)),
                workspace_id=str(task.workspace_id),
            )

            if not attempt.provider_task_id:
                if attempt.status != "ambiguous":
                    AITaskStateMachine.ensure_attempt_transition(
                        attempt.status,
                        "ambiguous",
                    )
                    attempt.status = "ambiguous"
                attempt.diagnostic = {
                    "billing_state": (
                        "acknowledged" if task.provider_billable else "unknown"
                    ),
                    "stage": "recovery",
                    "reason": "provider_task_id_missing",
                }
                return RecoveryClaim(
                    task_id=str(task.id),
                    status="ambiguous",
                    context=context,
                    attempt_id=str(attempt.id),
                    provider_billable=task.provider_billable,
                )

            now = datetime.now(timezone.utc)
            if (
                attempt.status == "polling"
                and _utc(task.updated_at) > now - self.stale_after
            ):
                return RecoveryClaim(task_id=str(task.id), status="polling")
            if attempt.status != "polling":
                AITaskStateMachine.ensure_attempt_transition(
                    attempt.status,
                    "polling",
                )
                attempt.status = "polling"
            task.updated_at = now
            task.provider_billable = True
            if attempt.billable_acknowledged_at is None:
                attempt.billable_acknowledged_at = now

            config_snapshot = copy.deepcopy(dict(attempt.config_snapshot))
            route = model_route_from_snapshot(config_snapshot)
            return RecoveryClaim(
                task_id=str(task.id),
                status="polling",
                lease=ProviderRecoveryLease(
                    task=WorkerTaskLease(
                        task_id=str(task.id),
                        attempt_id=str(attempt.id),
                        context=context,
                        project_id=str(task.project_id) if task.project_id else None,
                        capability=task.capability,
                        request_payload=copy.deepcopy(dict(task.request_payload)),
                        config_snapshot=config_snapshot,
                        model_route=route,
                    ),
                    provider_task_id=attempt.provider_task_id,
                    provider_request_id=attempt.provider_request_id,
                ),
            )


class AIWorkerRecoveryService:
    def __init__(
        self,
        *,
        recovery: AIRecoveryRepository,
        task_state: AITaskStateService,
        model_clients: WorkerModelClientFactory,
        provider_recovery: ProviderRecoveryInvoker,
        output_finalizer: WorkerOutputFinalizer | None = None,
        settlement: TicketSettlementService | None = None,
    ) -> None:
        self.recovery = recovery
        self.task_state = task_state
        self.model_clients = model_clients
        self.provider_recovery = provider_recovery
        self.output_finalizer = output_finalizer
        self.settlement = settlement

    def _support_review(
        self,
        *,
        context: WorkspaceContext,
        task_id: str,
        attempt_id: str,
        reason: str,
        error: Exception | None = None,
    ) -> RecoveryExecutionResult:
        if self.settlement is None:
            return RecoveryExecutionResult(
                task_id=task_id,
                attempt_id=attempt_id,
                status="ambiguous",
                recovered=False,
            )
        self.settlement.release_unmetered_billable_failure(
            context,
            task_id=task_id,
            attempt_id=attempt_id,
            support_review_reason=reason,
            safe_error_code="AI_TASK_RECOVERY_REVIEW_REQUIRED",
            safe_error_message="AI 任务无法自动恢复，预扣算力券已全部退回",
            attempt_diagnostic={
                "billing_state": "acknowledged",
                "error_type": type(error).__name__ if error is not None else None,
                "stage": "recovery",
                "reason": reason,
            },
        )
        return RecoveryExecutionResult(
            task_id=task_id,
            attempt_id=attempt_id,
            status="support_review",
            recovered=True,
        )

    def _ambiguous(
        self,
        lease: ProviderRecoveryLease,
        error: Exception,
    ) -> RecoveryExecutionResult:
        if self.settlement is not None:
            return self._support_review(
                context=lease.task.context,
                task_id=lease.task.task_id,
                attempt_id=lease.task.attempt_id,
                reason="供应商任务无法自动恢复",
                error=error,
            )
        attempt = self.task_state.transition_attempt(
            lease.task.context,
            task_id=lease.task.task_id,
            attempt_id=lease.task.attempt_id,
            expected_statuses={"polling"},
            target_status="ambiguous",
            diagnostic={
                "billing_state": "acknowledged",
                "error_type": type(error).__name__,
                "stage": "recovery_poll",
            },
        )
        return RecoveryExecutionResult(
            task_id=lease.task.task_id,
            attempt_id=lease.task.attempt_id,
            status=attempt.status,
            recovered=True,
        )

    def recover(self, task_id: str) -> RecoveryExecutionResult:
        claim = self.recovery.claim(task_id)
        lease = claim.lease
        if lease is None:
            if (
                claim.status == "ambiguous"
                and claim.context is not None
                and claim.attempt_id is not None
                and claim.provider_billable
                and self.settlement is not None
            ):
                return self._support_review(
                    context=claim.context,
                    task_id=claim.task_id,
                    attempt_id=claim.attempt_id,
                    reason="供应商未提供可恢复的任务标识",
                )
            return RecoveryExecutionResult(
                task_id=claim.task_id,
                status=claim.status,
                recovered=False,
            )
        try:
            client = self.model_clients.create(lease.task.model_route)
            outcome = self.provider_recovery.resume(client, lease)
            if not isinstance(outcome, ProviderRecoveryOutcome):
                raise TypeError("供应商恢复结果契约无效")
            if outcome.status not in {"pending", "succeeded"}:
                raise ValueError("供应商恢复状态无效")
        except Exception as exc:
            return self._ambiguous(lease, exc)

        if outcome.status == "pending":
            return RecoveryExecutionResult(
                task_id=lease.task.task_id,
                attempt_id=lease.task.attempt_id,
                status="polling",
                recovered=True,
            )

        try:
            normalized_usage = normalize_provider_usage(
                lease.task.model_route,
                outcome.raw_usage,
            )
            self.task_state.record_attempt_usage(
                lease.task.context,
                task_id=lease.task.task_id,
                attempt_id=lease.task.attempt_id,
                raw_usage=normalized_usage,
            )
            task = self.task_state.transition_task(
                lease.task.context,
                task_id=lease.task.task_id,
                expected_statuses={"running"},
                target_status="provider_succeeded",
                result=outcome.result,
            )
        except Exception as exc:
            return self._ambiguous(lease, exc)
        if self.output_finalizer is not None:
            finalized = self.output_finalizer.finalize(
                lease.task.context,
                lease.task.task_id,
            )
            return RecoveryExecutionResult(
                task_id=lease.task.task_id,
                attempt_id=lease.task.attempt_id,
                status=str(finalized.status),
                recovered=True,
            )
        return RecoveryExecutionResult(
            task_id=lease.task.task_id,
            attempt_id=lease.task.attempt_id,
            status=task.status,
            recovered=True,
        )
