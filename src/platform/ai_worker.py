from __future__ import annotations

import copy
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select, update

from .ai_task_state import AITaskStateConflictError, AITaskStateService
from .contracts import CredentialProvider, ModelRouteSnapshot, UserContext, WorkspaceContext
from .database import Database
from .db_models import AITaskAttemptRecord, AITaskRecord
from .metering import evaluate_metering_tokens
from .identifiers import parse_database_id
from .model_routing import RequestScopedModelClient, RequestScopedModelClientFactory
from .observability import events, metrics
from .ticket_settlement import TicketSettlementService


WORKER_IDENTITY = UserContext(
    user_id="0",
    session_id="ai-worker",
    is_platform_admin=True,
)


class AIWorkerError(RuntimeError):
    pass


class AIWorkerTaskNotFoundError(AIWorkerError):
    pass


class AIWorkerSnapshotError(AIWorkerError):
    pass


class WorkerModelClientFactory(Protocol):
    def create(self, snapshot: ModelRouteSnapshot) -> RequestScopedModelClient: ...


class ProviderInvoker(Protocol):
    def invoke(
        self,
        client: RequestScopedModelClient,
        task: "WorkerTaskLease",
        on_provider_submission: "ProviderSubmissionRecorder",
    ) -> "ProviderInvocationOutcome": ...


class WorkerInputResolver(Protocol):
    def resolve(
        self,
        context: WorkspaceContext,
        *,
        request_payload: Mapping[str, Any],
        project_id: str | None,
    ) -> tuple[Any, ...]: ...


class WorkerOutputFinalizer(Protocol):
    def finalize(self, context: WorkspaceContext, task_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProviderInvocationOutcome:
    raw_usage: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerTaskLease:
    task_id: str
    attempt_id: str
    context: WorkspaceContext
    project_id: str | None
    capability: str
    request_payload: dict[str, Any]
    config_snapshot: dict[str, Any]
    model_route: ModelRouteSnapshot
    provider_inputs: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerAcquisition:
    task_id: str
    status: str
    lease: WorkerTaskLease | None = None
    context: WorkspaceContext | None = None


@dataclass(frozen=True, slots=True)
class WorkerExecutionResult:
    task_id: str
    status: str
    acquired: bool
    attempt_id: str | None = None


class ProviderSubmissionRecorder:
    def __init__(
        self,
        task_state: AITaskStateService,
        lease: WorkerTaskLease,
    ) -> None:
        self.task_state = task_state
        self.lease = lease
        self.recorded = False
        self.billable_acknowledged = False

    def __call__(
        self,
        provider_name: str,
        provider_task_id: str | None,
        provider_request_id: str | None,
        billable_acknowledged: bool = True,
    ) -> None:
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise AIWorkerSnapshotError("供应商名称无效")
        self.task_state.record_provider_submission(
            self.lease.context,
            task_id=self.lease.task_id,
            attempt_id=self.lease.attempt_id,
            provider_request_id=provider_request_id,
            provider_task_id=provider_task_id,
            billable_acknowledged=billable_acknowledged,
        )
        self.recorded = True
        self.billable_acknowledged = (
            self.billable_acknowledged or billable_acknowledged
        )


def _required_text(snapshot: Mapping[str, Any], key: str) -> str:
    value = snapshot.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AIWorkerSnapshotError(f"任务配置快照缺少 {key}")
    return value.strip()


def _mapping(snapshot: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = snapshot.get(key)
    if not isinstance(value, Mapping):
        raise AIWorkerSnapshotError(f"任务配置快照中的 {key} 无效")
    return copy.deepcopy(dict(value))


def model_route_from_snapshot(snapshot: Mapping[str, Any]) -> ModelRouteSnapshot:
    return ModelRouteSnapshot(
        config_version_id=_required_text(snapshot, "config_version_id"),
        route_id=_required_text(snapshot, "route_id"),
        capability=_required_text(snapshot, "capability"),
        provider=_required_text(snapshot, "provider"),
        provider_model_id=_required_text(snapshot, "provider_model_id"),
        display_name=_required_text(snapshot, "display_name"),
        parameters=_mapping(snapshot, "parameters"),
        metering_formula=_mapping(snapshot, "metering_formula"),
        fallback_policy=_mapping(snapshot, "fallback_policy"),
        secret_ref=_required_text(snapshot, "secret_ref"),
    )


def normalize_provider_usage(
    route: ModelRouteSnapshot,
    raw_usage: Mapping[str, Any],
) -> dict[str, Any]:
    usage = copy.deepcopy(dict(raw_usage))
    formula = dict(route.metering_formula)
    parameters = dict(route.parameters)
    kind = formula.get("kind")
    if kind == "image":
        usage.update(
            {
                "output_count": parameters.get(
                    "output_count",
                    parameters.get("count", 1),
                ),
                "resolution": parameters.get("resolution", parameters.get("size")),
            }
        )
    elif kind == "video":
        usage.update(
            {
                "audio": any(
                    bool(parameters.get(name, False))
                    for name in ("generate_audio", "audio", "sound", "vidu_audio")
                ),
                "duration_seconds": parameters.get("duration"),
                "output_count": parameters.get(
                    "output_count",
                    parameters.get("count", 1),
                ),
                "resolution": parameters.get("resolution", parameters.get("size")),
            }
        )
    evaluate_metering_tokens(
        formula,
        parameters=parameters,
        raw_usage=usage,
    )
    return usage


class AIWorkerTaskRepository:
    def __init__(
        self,
        database: Database,
        *,
        worker_identity: UserContext = WORKER_IDENTITY,
    ) -> None:
        self.database = database
        self.worker_identity = worker_identity

    @staticmethod
    def _task_id(value: str) -> int:
        try:
            return parse_database_id(value, field="任务 ID")
        except ValueError as exc:
            raise AIWorkerTaskNotFoundError("AI 任务不存在") from exc

    def acquire(self, task_id: str) -> WorkerAcquisition:
        canonical_task_id = self._task_id(task_id)
        with self.database.transaction(self.worker_identity) as session:
            task = session.scalar(
                select(AITaskRecord)
                .where(AITaskRecord.id == canonical_task_id)
                .with_for_update()
            )
            if task is None:
                raise AIWorkerTaskNotFoundError("AI 任务不存在")
            context = WorkspaceContext(
                identity=UserContext(user_id=str(task.user_id)),
                workspace_id=str(task.workspace_id),
            )
            if task.status != "queued":
                return WorkerAcquisition(
                    task_id=str(task.id),
                    status=task.status,
                    context=context,
                )

            attempt = session.scalar(
                select(AITaskAttemptRecord)
                .where(
                    AITaskAttemptRecord.task_id == task.id,
                    AITaskAttemptRecord.status == "pending",
                )
                .order_by(AITaskAttemptRecord.attempt_number)
                .with_for_update()
            )
            if attempt is None:
                raise AIWorkerSnapshotError("排队任务缺少可执行尝试")

            now = datetime.now(timezone.utc)
            created_at = task.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            task_update = session.execute(
                update(AITaskRecord)
                .where(
                    AITaskRecord.id == task.id,
                    AITaskRecord.status == "queued",
                )
                .values(status="running", started_at=now, updated_at=now)
            )
            attempt_update = session.execute(
                update(AITaskAttemptRecord)
                .where(
                    AITaskAttemptRecord.id == attempt.id,
                    AITaskAttemptRecord.task_id == task.id,
                    AITaskAttemptRecord.status == "pending",
                )
                .values(status="running", started_at=now)
            )
            if task_update.rowcount != 1 or attempt_update.rowcount != 1:
                raise AITaskStateConflictError("AI 任务已被其他执行者领取")

            metrics.increment(
                "ai_task_transitions_total",
                labels={"capability": task.capability, "status": "running"},
            )
            metrics.observe(
                "ai_task_state_latency_seconds",
                max((now - created_at).total_seconds(), 0),
                labels={"capability": task.capability, "status": "queued_to_running"},
            )

            config_snapshot = copy.deepcopy(dict(attempt.config_snapshot))
            route = model_route_from_snapshot(config_snapshot)
            return WorkerAcquisition(
                task_id=str(task.id),
                status="running",
                context=context,
                lease=WorkerTaskLease(
                    task_id=str(task.id),
                    attempt_id=str(attempt.id),
                    context=context,
                    project_id=str(task.project_id) if task.project_id else None,
                    capability=task.capability,
                    request_payload=copy.deepcopy(dict(task.request_payload)),
                    config_snapshot=config_snapshot,
                    model_route=route,
                ),
            )


class AIWorkerService:
    def __init__(
        self,
        *,
        tasks: AIWorkerTaskRepository,
        task_state: AITaskStateService,
        settlement: TicketSettlementService,
        model_clients: WorkerModelClientFactory,
        provider_invoker: ProviderInvoker,
        input_resolver: WorkerInputResolver | None = None,
        output_finalizer: WorkerOutputFinalizer | None = None,
    ) -> None:
        self.tasks = tasks
        self.task_state = task_state
        self.settlement = settlement
        self.model_clients = model_clients
        self.provider_invoker = provider_invoker
        self.input_resolver = input_resolver
        self.output_finalizer = output_finalizer

    def _fail_nonbillable(
        self,
        lease: WorkerTaskLease,
        *,
        stage: str,
        error: Exception,
    ) -> WorkerExecutionResult:
        error_code = (
            "MODEL_CLIENT_UNAVAILABLE"
            if stage == "model_client"
            else (
                "PROVIDER_INPUT_UNAVAILABLE"
                if stage == "provider_input"
                else "PROVIDER_INVOCATION_FAILED"
            )
        )
        message = (
            "模型服务暂时不可用，请稍后重试"
            if stage == "model_client"
            else (
                "AI 输入资源不可用，请刷新后重试"
                if stage == "provider_input"
                else "AI 生成失败，请稍后重试"
            )
        )
        self.settlement.release_nonbillable(
            lease.context,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            outcome="nonbillable_failure",
            reason="AI 任务在供应商确认计费前失败",
            safe_error_code=error_code,
            safe_error_message=message,
            attempt_diagnostic={
                "billing_state": "not_billable",
                "error_type": type(error).__name__,
                "stage": stage,
            },
        )
        metrics.increment(
            "ai_worker_results_total",
            labels={"capability": lease.capability, "outcome": "nonbillable_failure"},
        )
        events.emit(
            "ai.worker_failed",
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            capability=lease.capability,
            stage=stage,
            error_type=type(error).__name__,
        )
        return WorkerExecutionResult(
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            status="failed",
            acquired=True,
        )

    def _mark_ambiguous(
        self,
        lease: WorkerTaskLease,
        *,
        stage: str,
        error: Exception,
    ) -> WorkerExecutionResult:
        attempt = self.task_state.transition_attempt(
            lease.context,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            expected_statuses={"running", "polling"},
            target_status="ambiguous",
            diagnostic={
                "billing_state": "acknowledged",
                "error_type": type(error).__name__,
                "stage": stage,
            },
        )
        metrics.increment(
            "ai_worker_results_total",
            labels={"capability": lease.capability, "outcome": "ambiguous"},
        )
        events.emit(
            "ai.worker_ambiguous",
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            capability=lease.capability,
            stage=stage,
            error_type=type(error).__name__,
        )
        return WorkerExecutionResult(
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            status=attempt.status,
            acquired=True,
        )

    def execute(self, task_id: str) -> WorkerExecutionResult:
        execution_started_at = time.perf_counter()
        acquisition = self.tasks.acquire(task_id)
        lease = acquisition.lease
        if lease is None:
            if (
                acquisition.status == "provider_succeeded"
                and acquisition.context is not None
                and self.output_finalizer is not None
            ):
                finalized = self.output_finalizer.finalize(
                    acquisition.context,
                    acquisition.task_id,
                )
                return WorkerExecutionResult(
                    task_id=acquisition.task_id,
                    status=str(finalized.status),
                    acquired=False,
                )
            return WorkerExecutionResult(
                task_id=acquisition.task_id,
                status=acquisition.status,
                acquired=False,
            )

        if self.input_resolver is not None:
            try:
                provider_inputs = self.input_resolver.resolve(
                    lease.context,
                    request_payload=lease.request_payload,
                    project_id=lease.project_id,
                )
                lease = replace(lease, provider_inputs=provider_inputs)
            except Exception as exc:
                return self._fail_nonbillable(
                    lease,
                    stage="provider_input",
                    error=exc,
                )

        try:
            client = self.model_clients.create(lease.model_route)
        except Exception as exc:
            return self._fail_nonbillable(
                lease,
                stage="model_client",
                error=exc,
            )

        submission = ProviderSubmissionRecorder(self.task_state, lease)
        provider_started_at = time.perf_counter()
        try:
            outcome = self.provider_invoker.invoke(client, lease, submission)
            if not isinstance(outcome, ProviderInvocationOutcome):
                raise TypeError("供应商调用结果契约无效")
            normalized_usage = normalize_provider_usage(
                lease.model_route,
                outcome.raw_usage,
            )
            metering_tokens = evaluate_metering_tokens(
                lease.model_route.metering_formula,
                parameters=lease.model_route.parameters,
                raw_usage=normalized_usage,
            )
        except Exception as exc:
            metrics.observe(
                "ai_provider_request_duration_seconds",
                max(time.perf_counter() - provider_started_at, 0),
                labels={
                    "capability": lease.capability,
                    "provider": lease.model_route.provider,
                    "outcome": "failed",
                },
            )
            if submission.billable_acknowledged:
                return self._mark_ambiguous(
                    lease,
                    stage="provider_invocation",
                    error=exc,
                )
            return self._fail_nonbillable(
                lease,
                stage="provider_invocation",
                error=exc,
            )

        metrics.observe(
            "ai_provider_request_duration_seconds",
            max(time.perf_counter() - provider_started_at, 0),
            labels={
                "capability": lease.capability,
                "provider": lease.model_route.provider,
                "outcome": "succeeded",
            },
        )
        metrics.increment(
            "provider_usage_metering_tokens_total",
            metering_tokens,
            labels={
                "capability": lease.capability,
                "provider": lease.model_route.provider,
                "outcome": "accepted",
            },
        )

        self.task_state.transition_attempt(
            lease.context,
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            expected_statuses={"running"},
            target_status="polling",
            raw_usage=normalized_usage,
        )
        task = self.task_state.transition_task(
            lease.context,
            task_id=lease.task_id,
            expected_statuses={"running"},
            target_status="provider_succeeded",
            result=outcome.result,
        )
        metrics.increment(
            "ai_task_transitions_total",
            labels={"capability": lease.capability, "status": "provider_succeeded"},
        )
        metrics.observe(
            "ai_worker_execution_duration_seconds",
            max(time.perf_counter() - execution_started_at, 0),
            labels={"capability": lease.capability, "outcome": "provider_succeeded"},
        )
        events.emit(
            "ai.worker_provider_succeeded",
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            capability=lease.capability,
            provider=lease.model_route.provider,
            metering_tokens=metering_tokens,
        )
        if self.output_finalizer is not None:
            finalized = self.output_finalizer.finalize(
                lease.context,
                lease.task_id,
            )
            return WorkerExecutionResult(
                task_id=lease.task_id,
                attempt_id=lease.attempt_id,
                status=str(finalized.status),
                acquired=True,
            )
        return WorkerExecutionResult(
            task_id=lease.task_id,
            attempt_id=lease.attempt_id,
            status=task.status,
            acquired=True,
        )


def build_ai_worker_service(
    database: Database,
    *,
    credential_provider: CredentialProvider,
    provider_invoker: ProviderInvoker,
    input_resolver: WorkerInputResolver,
    output_finalizer: WorkerOutputFinalizer,
) -> AIWorkerService:
    return AIWorkerService(
        tasks=AIWorkerTaskRepository(database),
        task_state=AITaskStateService(database),
        settlement=TicketSettlementService(database),
        model_clients=RequestScopedModelClientFactory(credential_provider),
        provider_invoker=provider_invoker,
        input_resolver=input_resolver,
        output_finalizer=output_finalizer,
    )
