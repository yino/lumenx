from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Protocol

from sqlalchemy import select

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


class ProviderRecoveryInvoker(Protocol):
    def resume(
        self,
        client: RequestScopedModelClient,
        task: "ProviderRecoveryLease",
    ) -> "ProviderRecoveryOutcome": ...


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
    ) -> None:
        if stale_after_seconds < 0:
            raise ValueError("恢复租约过期时间不能为负数")
        self.database = database
        self.worker_identity = worker_identity
        self.stale_after = timedelta(seconds=stale_after_seconds)

    @staticmethod
    def _task_id(value: str) -> int:
        try:
            return parse_database_id(value, field="任务 ID")
        except ValueError as exc:
            raise ValueError("AI 任务标识无效") from exc

    def list_candidate_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("恢复任务数量必须为正整数")
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
                return RecoveryClaim(task_id=str(task.id), status="ambiguous")

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
            context = WorkspaceContext(
                identity=UserContext(user_id=str(task.user_id)),
                workspace_id=str(task.workspace_id),
            )
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
    ) -> None:
        self.recovery = recovery
        self.task_state = task_state
        self.model_clients = model_clients
        self.provider_recovery = provider_recovery
        self.output_finalizer = output_finalizer

    def _ambiguous(
        self,
        lease: ProviderRecoveryLease,
        error: Exception,
    ) -> RecoveryExecutionResult:
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
