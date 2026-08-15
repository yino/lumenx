from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import time
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai_task_state import AITaskStateMachine
from .contracts import WorkspaceContext
from .database import Database
from .db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    TicketHoldRecord,
    TicketWalletRecord,
    UsageEventRecord,
)
from .identifiers import parse_database_id, parse_optional_database_id
from .metering import MeteringValidationError, evaluate_metering_tokens
from .observability import events, metrics
from .ticket_math import TicketArithmeticError, metering_tokens_to_microtickets
from .ticket_wallet import TicketWalletService, TicketWalletSnapshot


class TicketSettlementError(RuntimeError):
    pass


class TicketSettlementConflictError(TicketSettlementError):
    pass


class TicketCancellationPendingError(TicketSettlementConflictError):
    """Cancellation is recorded, but billing cannot be released yet."""


class TicketSettlementScopeNotFoundError(TicketSettlementError):
    pass


@dataclass(frozen=True, slots=True)
class SuccessfulSettlement:
    task_id: str
    attempt_id: str | None
    usage_event_id: str
    metering_tokens: int
    charged_microtickets: int
    released_microtickets: int
    tokens_per_ticket: int
    wallet: TicketWalletSnapshot
    reused: bool = False


FailureOutcome = Literal["nonbillable_failure", "billable_failure", "cancelled"]


@dataclass(frozen=True, slots=True)
class FailureSettlement:
    task_id: str
    attempt_id: str | None
    usage_event_id: str
    outcome: FailureOutcome
    metering_tokens: int
    charged_microtickets: int
    released_microtickets: int
    wallet: TicketWalletSnapshot
    support_review: bool = False
    reused: bool = False


def observe_settlement(operation: str):
    def decorator(function):
        @wraps(function)
        def wrapped(self, context, *args, **kwargs):
            started_at = time.perf_counter()
            try:
                result = function(self, context, *args, **kwargs)
            except Exception as exc:
                metrics.increment(
                    "ticket_settlement_total",
                    labels={"operation": operation, "outcome": "failed"},
                )
                metrics.increment(
                    "ticket_settlement_failures_total",
                    labels={"operation": operation, "error_code": type(exc).__name__},
                )
                events.emit(
                    "ticket.settlement_failed",
                    task_id=str(kwargs.get("task_id", "unknown")),
                    operation=operation,
                    error_type=type(exc).__name__,
                )
                raise
            outcome = getattr(result, "outcome", "succeeded")
            reused = bool(getattr(result, "reused", False))
            metrics.increment(
                "ticket_settlement_total",
                labels={
                    "operation": operation,
                    "outcome": "reused" if reused else str(outcome),
                },
            )
            metrics.observe(
                "ticket_settlement_duration_seconds",
                max(time.perf_counter() - started_at, 0),
                labels={"operation": operation, "outcome": str(outcome)},
            )
            if not reused and getattr(result, "metering_tokens", 0):
                metrics.increment(
                    "provider_usage_metering_tokens_total",
                    float(result.metering_tokens),
                    labels={"capability": "settled", "outcome": str(outcome)},
                )
            return result

        return wrapped

    return decorator


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            copy.deepcopy(dict(value)),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise TicketSettlementConflictError(f"{name}必须是有效的 JSON 对象") from exc
    if not isinstance(decoded, dict):
        raise TicketSettlementConflictError(f"{name}必须是有效的 JSON 对象")
    return decoded


class TicketSettlementService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _id(value: str | int, name: str) -> int:
        try:
            return parse_database_id(value, field=name)
        except ValueError as exc:
            raise TicketSettlementConflictError(f"{name}无效") from exc

    @staticmethod
    def _optional_id(value: str | int | None, name: str) -> int | None:
        try:
            return parse_optional_database_id(value, field=name)
        except ValueError as exc:
            raise TicketSettlementConflictError(f"{name}无效") from exc

    @staticmethod
    def _reason(value: str) -> str:
        if not isinstance(value, str):
            raise TicketSettlementConflictError("账务变更必须填写原因")
        normalized = value.strip()
        if not normalized:
            raise TicketSettlementConflictError("账务变更必须填写原因")
        if len(normalized) > 2000:
            raise TicketSettlementConflictError("账务变更原因不能超过 2000 个字符")
        return normalized

    @staticmethod
    def _require_task_for_update(
        session: Session,
        *,
        user_id: int,
        workspace_id: int,
        task_id: int,
    ) -> AITaskRecord:
        task = session.scalar(
            select(AITaskRecord)
            .where(
                AITaskRecord.id == task_id,
                AITaskRecord.user_id == user_id,
                AITaskRecord.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if task is None:
            raise TicketSettlementScopeNotFoundError("AI 任务不存在")
        return task

    @staticmethod
    def _require_hold_for_update(
        session: Session,
        task: AITaskRecord,
        attempt: AITaskAttemptRecord | None,
    ) -> TicketHoldRecord:
        statement = select(TicketHoldRecord).where(
            TicketHoldRecord.user_id == task.user_id,
            TicketHoldRecord.workspace_id == task.workspace_id,
            TicketHoldRecord.task_id == task.id,
        )
        if attempt is not None:
            statement = statement.where(TicketHoldRecord.attempt_id == attempt.id)
        holds = list(session.scalars(statement.with_for_update()))
        if not holds:
            raise TicketSettlementConflictError("AI 任务缺少算力券预扣记录")
        if len(holds) != 1:
            raise TicketSettlementConflictError(
                "AI 任务存在多个预扣，必须指定尝试标识"
            )
        return holds[0]

    @staticmethod
    def _require_attempt_for_update(
        session: Session,
        task: AITaskRecord,
        attempt_id: int | None,
    ) -> AITaskAttemptRecord | None:
        if attempt_id is None:
            return None
        attempt = session.scalar(
            select(AITaskAttemptRecord)
            .where(
                AITaskAttemptRecord.id == attempt_id,
                AITaskAttemptRecord.user_id == task.user_id,
                AITaskAttemptRecord.workspace_id == task.workspace_id,
                AITaskAttemptRecord.task_id == task.id,
            )
            .with_for_update()
        )
        if attempt is None:
            raise TicketSettlementScopeNotFoundError("AI 任务尝试不存在")
        return attempt

    @staticmethod
    def _successful_usage(
        session: Session,
        task: AITaskRecord,
        attempt: AITaskAttemptRecord | None,
    ) -> UsageEventRecord | None:
        statement = select(UsageEventRecord).where(
            UsageEventRecord.user_id == task.user_id,
            UsageEventRecord.workspace_id == task.workspace_id,
            UsageEventRecord.task_id == task.id,
            UsageEventRecord.outcome == "succeeded",
        )
        if attempt is not None:
            statement = statement.where(UsageEventRecord.attempt_id == attempt.id)
        return session.scalar(
            statement.order_by(
                UsageEventRecord.created_at.desc(),
                UsageEventRecord.id.desc(),
            )
        )

    @staticmethod
    def _failure_usage(
        session: Session,
        task: AITaskRecord,
        attempt: AITaskAttemptRecord | None,
        outcome: FailureOutcome,
    ) -> UsageEventRecord | None:
        statement = select(UsageEventRecord).where(
            UsageEventRecord.user_id == task.user_id,
            UsageEventRecord.workspace_id == task.workspace_id,
            UsageEventRecord.task_id == task.id,
            UsageEventRecord.outcome == outcome,
        )
        if attempt is not None:
            statement = statement.where(UsageEventRecord.attempt_id == attempt.id)
        return session.scalar(
            statement.order_by(
                UsageEventRecord.created_at.desc(),
                UsageEventRecord.id.desc(),
            )
        )

    @staticmethod
    def _snapshot_parts(
        task: AITaskRecord,
        attempt: AITaskAttemptRecord | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        source = attempt.config_snapshot if attempt is not None else task.config_snapshot
        snapshot = _json_object(source, "任务配置快照")
        formula = snapshot.get("metering_formula")
        if not isinstance(formula, dict):
            raise TicketSettlementConflictError("任务配置快照缺少计量公式")
        capability = snapshot.get("capability")
        if capability is not None and capability != task.capability:
            raise TicketSettlementConflictError("任务能力与配置快照不一致")
        parameters = snapshot.get("parameters", task.request_payload.get("parameters", {}))
        if not isinstance(parameters, dict):
            raise TicketSettlementConflictError("任务配置快照中的参数无效")
        return formula, parameters

    @staticmethod
    def _result_from_existing(
        session: Session,
        task: AITaskRecord,
        hold: TicketHoldRecord,
        usage: UsageEventRecord,
    ) -> SuccessfulSettlement:
        wallet = session.scalar(
            select(TicketWalletRecord).where(
                TicketWalletRecord.user_id == task.user_id
            )
        )
        if wallet is None:
            raise TicketSettlementConflictError("AI 任务缺少算力券钱包")
        return SuccessfulSettlement(
            task_id=str(task.id),
            attempt_id=str(usage.attempt_id) if usage.attempt_id else None,
            usage_event_id=str(usage.id),
            metering_tokens=usage.metering_tokens,
            charged_microtickets=usage.charged_microtickets,
            released_microtickets=(
                hold.quoted_microtickets - usage.charged_microtickets
            ),
            tokens_per_ticket=usage.tokens_per_ticket,
            wallet=TicketWalletService._snapshot(wallet),
            reused=True,
        )

    @staticmethod
    def _failure_result_from_existing(
        session: Session,
        task: AITaskRecord,
        hold: TicketHoldRecord,
        usage: UsageEventRecord,
        outcome: FailureOutcome,
    ) -> FailureSettlement:
        wallet = session.scalar(
            select(TicketWalletRecord).where(
                TicketWalletRecord.user_id == task.user_id
            )
        )
        if wallet is None:
            raise TicketSettlementConflictError("AI 任务缺少算力券钱包")
        return FailureSettlement(
            task_id=str(task.id),
            attempt_id=str(usage.attempt_id) if usage.attempt_id else None,
            usage_event_id=str(usage.id),
            outcome=outcome,
            metering_tokens=usage.metering_tokens,
            charged_microtickets=usage.charged_microtickets,
            released_microtickets=(
                hold.quoted_microtickets - usage.charged_microtickets
            ),
            wallet=TicketWalletService._snapshot(wallet),
            support_review=task.status == "support_review",
            reused=True,
        )

    @staticmethod
    def _metering_formula_for_history(
        task: AITaskRecord,
        attempt: AITaskAttemptRecord | None,
    ) -> dict[str, Any]:
        source = attempt.config_snapshot if attempt is not None else task.config_snapshot
        snapshot = _json_object(source, "任务配置快照")
        formula = snapshot.get("metering_formula")
        return formula if isinstance(formula, dict) else {}

    @observe_settlement("success")
    def settle_success(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        raw_provider_usage: Mapping[str, Any],
        attempt_id: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> SuccessfulSettlement:
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        canonical_task_id = self._id(task_id, "任务标识")
        canonical_attempt_id = self._optional_id(attempt_id, "尝试标识")
        normalized_result = (
            _json_object(result, "任务结果") if result is not None else None
        )

        with self.database.transaction(context.identity) as session:
            task = self._require_task_for_update(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            attempt = self._require_attempt_for_update(
                session,
                task,
                canonical_attempt_id,
            )
            hold = self._require_hold_for_update(session, task, attempt)
            existing_usage = self._successful_usage(session, task, attempt)
            if task.status == "succeeded":
                if existing_usage is None:
                    raise TicketSettlementConflictError("已完成任务缺少成功用量记录")
                if normalized_result is not None and task.result != normalized_result:
                    raise TicketSettlementConflictError("已完成任务的结果与重试请求不一致")
                return self._result_from_existing(session, task, hold, existing_usage)
            if existing_usage is not None:
                raise TicketSettlementConflictError("任务状态与成功用量记录不一致")
            if task.status != "provider_succeeded":
                raise TicketSettlementConflictError("AI 任务尚未达到可结算状态")
            if hold.status != "held" or hold.remaining_microtickets != hold.quoted_microtickets:
                raise TicketSettlementConflictError("算力券预扣状态与任务报价不一致")

            normalized_raw_usage = _json_object(raw_provider_usage, "供应商用量")
            formula, parameters = self._snapshot_parts(task, attempt)
            try:
                metering_tokens = evaluate_metering_tokens(
                    formula,
                    parameters=parameters,
                    raw_usage=normalized_raw_usage,
                )
                charged_microtickets = metering_tokens_to_microtickets(
                    metering_tokens,
                    task.tokens_per_ticket,
                )
            except (MeteringValidationError, TicketArithmeticError) as exc:
                raise TicketSettlementConflictError(str(exc)) from exc
            if charged_microtickets > hold.remaining_microtickets:
                raise TicketSettlementConflictError("实际用量超过任务预扣上限")

            usage_event = UsageEventRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=attempt.id if attempt is not None else None,
                capability=task.capability,
                outcome="succeeded",
                raw_provider_usage=normalized_raw_usage,
                metering_formula=formula,
                metering_tokens=metering_tokens,
                tokens_per_ticket=task.tokens_per_ticket,
                charged_microtickets=charged_microtickets,
            )
            session.add(usage_event)
            session.flush()
            usage_event_id = usage_event.id
            released_microtickets = (
                hold.remaining_microtickets - charged_microtickets
            )
            wallet = TicketWalletService.require_wallet_for_update(session, user_id)
            wallet_snapshot = TicketWalletService.settle_in_session(
                session,
                wallet,
                charged_microtickets,
                reason="AI 任务实际结算",
                workspace_id=workspace_id,
                project_id=task.project_id,
                task_id=task.id,
                hold_id=hold.id,
                correlation={
                    "metering_tokens": metering_tokens,
                    "tokens_per_ticket": task.tokens_per_ticket,
                    "usage_event_id": str(usage_event_id),
                },
            )
            if released_microtickets:
                wallet_snapshot = TicketWalletService.release_in_session(
                    session,
                    wallet,
                    released_microtickets,
                    reason="AI 任务释放未使用预扣",
                    workspace_id=workspace_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    hold_id=hold.id,
                    correlation={"usage_event_id": str(usage_event_id)},
                )

            now = datetime.now(timezone.utc)
            hold.remaining_microtickets = 0
            hold.status = "settled"
            hold.settled_at = now
            AITaskStateMachine.ensure_task_transition(task.status, "succeeded")
            task.status = "succeeded"
            task.provider_billable = True
            if normalized_result is not None:
                task.result = normalized_result
            task.completed_at = now
            if attempt is not None:
                AITaskStateMachine.ensure_attempt_transition(
                    attempt.status,
                    "succeeded",
                )
                attempt.status = "succeeded"
                attempt.raw_usage = normalized_raw_usage
                attempt.completed_at = now
            session.flush()
            return SuccessfulSettlement(
                task_id=str(task.id),
                attempt_id=str(attempt.id) if attempt is not None else None,
                usage_event_id=str(usage_event_id),
                metering_tokens=metering_tokens,
                charged_microtickets=charged_microtickets,
                released_microtickets=released_microtickets,
                tokens_per_ticket=task.tokens_per_ticket,
                wallet=wallet_snapshot,
            )

    @observe_settlement("release")
    def release_nonbillable(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        outcome: Literal["nonbillable_failure", "cancelled"],
        reason: str,
        safe_error_code: str | None = None,
        safe_error_message: str | None = None,
        attempt_id: str | None = None,
        attempt_diagnostic: Mapping[str, Any] | None = None,
    ) -> FailureSettlement:
        if outcome not in {"nonbillable_failure", "cancelled"}:
            raise TicketSettlementConflictError("非计费结算结果无效")
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        canonical_task_id = self._id(task_id, "任务标识")
        canonical_attempt_id = self._optional_id(attempt_id, "尝试标识")
        normalized_diagnostic = (
            _json_object(attempt_diagnostic, "任务尝试诊断")
            if attempt_diagnostic is not None
            else None
        )

        with self.database.transaction(context.identity) as session:
            task = self._require_task_for_update(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            attempt = self._require_attempt_for_update(
                session,
                task,
                canonical_attempt_id,
            )
            hold = self._require_hold_for_update(session, task, attempt)
            existing_usage = self._failure_usage(session, task, attempt, outcome)
            terminal_status = "failed" if outcome == "nonbillable_failure" else "cancelled"
            if task.status == terminal_status:
                if existing_usage is None:
                    raise TicketSettlementConflictError("终态任务缺少失败用量记录")
                return self._failure_result_from_existing(
                    session,
                    task,
                    hold,
                    existing_usage,
                    outcome,
                )
            if existing_usage is not None:
                raise TicketSettlementConflictError("任务状态与失败用量记录不一致")
            if task.status not in {"reserved", "queued", "running"}:
                raise TicketSettlementConflictError("AI 任务当前状态不能全额释放预扣")
            if task.provider_billable:
                if outcome == "cancelled":
                    raise TicketCancellationPendingError(
                        "供应商计费状态尚未结算，已记录取消请求"
                    )
                raise TicketSettlementConflictError(
                    "供应商已确认计费，不能全额释放预扣"
                )
            if outcome == "cancelled":
                attempts = list(
                    session.scalars(
                        select(AITaskAttemptRecord)
                        .where(
                            AITaskAttemptRecord.user_id == user_id,
                            AITaskAttemptRecord.workspace_id == workspace_id,
                            AITaskAttemptRecord.task_id == task.id,
                        )
                        .order_by(AITaskAttemptRecord.attempt_number)
                        .with_for_update()
                    )
                )
                open_holds = list(
                    session.scalars(
                        select(TicketHoldRecord)
                        .where(
                            TicketHoldRecord.user_id == user_id,
                            TicketHoldRecord.workspace_id == workspace_id,
                            TicketHoldRecord.task_id == task.id,
                            TicketHoldRecord.status == "held",
                        )
                        .with_for_update()
                    )
                )
                provider_task_known = any(
                    item.provider_task_id is not None
                    or item.billable_acknowledged_at is not None
                    for item in attempts
                )
                if provider_task_known or len(open_holds) != 1:
                    raise TicketCancellationPendingError(
                        "供应商状态尚未确认，已记录取消请求"
                    )
                if task.status in {"reserved", "queued"} and any(
                    item.provider_request_id is not None for item in attempts
                ):
                    raise TicketCancellationPendingError(
                        "供应商状态尚未确认，已记录取消请求"
                    )
                if task.status == "running":
                    latest_attempt = attempts[-1] if attempts else None
                    diagnostic = (
                        dict(latest_attempt.diagnostic or {})
                        if latest_attempt is not None
                        else dict(normalized_diagnostic or {})
                    )
                    if diagnostic.get("billing_state") != "not_billable":
                        raise TicketCancellationPendingError(
                            "供应商尚未确认未计费，已记录取消请求"
                        )
            if hold.status != "held":
                raise TicketSettlementConflictError("算力券预扣已经结束")

            normalized_reason = self._reason(reason)
            metering_formula = self._metering_formula_for_history(task, attempt)
            usage_event = UsageEventRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=attempt.id if attempt is not None else None,
                capability=task.capability,
                outcome=outcome,
                raw_provider_usage={},
                metering_formula=metering_formula,
                metering_tokens=0,
                tokens_per_ticket=task.tokens_per_ticket,
                charged_microtickets=0,
            )
            session.add(usage_event)
            session.flush()
            usage_event_id = usage_event.id
            released_microtickets = hold.remaining_microtickets
            wallet = TicketWalletService.require_wallet_for_update(session, user_id)
            wallet_snapshot = TicketWalletService.release_in_session(
                session,
                wallet,
                released_microtickets,
                reason=normalized_reason,
                workspace_id=workspace_id,
                project_id=task.project_id,
                task_id=task.id,
                hold_id=hold.id,
                correlation={
                    "outcome": outcome,
                    "usage_event_id": str(usage_event_id),
                },
            )

            now = datetime.now(timezone.utc)
            hold.remaining_microtickets = 0
            hold.status = "released"
            hold.released_at = now
            AITaskStateMachine.ensure_task_transition(task.status, terminal_status)
            task.status = terminal_status
            task.completed_at = now
            if outcome == "cancelled" and task.cancellation_requested_at is None:
                task.cancellation_requested_at = now
            task.safe_error_code = safe_error_code
            task.safe_error_message = safe_error_message
            task.result = None
            if attempt is not None:
                attempt_terminal_status = (
                    "cancelled" if outcome == "cancelled" else "failed"
                )
                AITaskStateMachine.ensure_attempt_transition(
                    attempt.status,
                    attempt_terminal_status,
                )
                attempt.status = attempt_terminal_status
                if normalized_diagnostic is not None:
                    attempt.diagnostic = normalized_diagnostic
                attempt.completed_at = now
            session.flush()
            return FailureSettlement(
                task_id=str(task.id),
                attempt_id=str(attempt.id) if attempt is not None else None,
                usage_event_id=str(usage_event_id),
                outcome=outcome,
                metering_tokens=0,
                charged_microtickets=0,
                released_microtickets=released_microtickets,
                wallet=wallet_snapshot,
            )

    @observe_settlement("billable_failure")
    def settle_billable_failure(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        raw_provider_usage: Mapping[str, Any],
        support_review_reason: str,
        safe_error_code: str | None = None,
        safe_error_message: str | None = None,
        attempt_id: str | None = None,
    ) -> FailureSettlement:
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        canonical_task_id = self._id(task_id, "任务标识")
        canonical_attempt_id = self._optional_id(attempt_id, "尝试标识")

        with self.database.transaction(context.identity) as session:
            task = self._require_task_for_update(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            attempt = self._require_attempt_for_update(
                session,
                task,
                canonical_attempt_id,
            )
            hold = self._require_hold_for_update(session, task, attempt)
            existing_usage = self._failure_usage(
                session,
                task,
                attempt,
                "billable_failure",
            )
            if task.status == "support_review":
                if existing_usage is None:
                    raise TicketSettlementConflictError("待复核任务缺少计费用量记录")
                return self._failure_result_from_existing(
                    session,
                    task,
                    hold,
                    existing_usage,
                    "billable_failure",
                )
            if existing_usage is not None:
                raise TicketSettlementConflictError("任务状态与计费失败记录不一致")
            if task.status != "provider_succeeded":
                raise TicketSettlementConflictError("AI 任务尚未达到计费失败结算状态")
            if hold.status != "held" or hold.remaining_microtickets != hold.quoted_microtickets:
                raise TicketSettlementConflictError("算力券预扣状态与任务报价不一致")

            normalized_review_reason = self._reason(support_review_reason)
            normalized_raw_usage = _json_object(raw_provider_usage, "供应商用量")
            formula, parameters = self._snapshot_parts(task, attempt)
            try:
                metering_tokens = evaluate_metering_tokens(
                    formula,
                    parameters=parameters,
                    raw_usage=normalized_raw_usage,
                )
                charged_microtickets = metering_tokens_to_microtickets(
                    metering_tokens,
                    task.tokens_per_ticket,
                )
            except (MeteringValidationError, TicketArithmeticError) as exc:
                raise TicketSettlementConflictError(str(exc)) from exc
            if charged_microtickets > hold.remaining_microtickets:
                raise TicketSettlementConflictError("实际用量超过任务预扣上限")

            usage_event = UsageEventRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=task.project_id,
                task_id=task.id,
                attempt_id=attempt.id if attempt is not None else None,
                capability=task.capability,
                outcome="billable_failure",
                raw_provider_usage=normalized_raw_usage,
                metering_formula=formula,
                metering_tokens=metering_tokens,
                tokens_per_ticket=task.tokens_per_ticket,
                charged_microtickets=charged_microtickets,
            )
            session.add(usage_event)
            session.flush()
            usage_event_id = usage_event.id
            released_microtickets = hold.remaining_microtickets - charged_microtickets
            wallet = TicketWalletService.require_wallet_for_update(session, user_id)
            wallet_snapshot = TicketWalletService.settle_in_session(
                session,
                wallet,
                charged_microtickets,
                reason="供应商已计费的失败任务结算",
                workspace_id=workspace_id,
                project_id=task.project_id,
                task_id=task.id,
                hold_id=hold.id,
                correlation={
                    "metering_tokens": metering_tokens,
                    "outcome": "billable_failure",
                    "tokens_per_ticket": task.tokens_per_ticket,
                    "usage_event_id": str(usage_event_id),
                },
            )
            if released_microtickets:
                wallet_snapshot = TicketWalletService.release_in_session(
                    session,
                    wallet,
                    released_microtickets,
                    reason="计费失败任务释放未使用预扣",
                    workspace_id=workspace_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    hold_id=hold.id,
                    correlation={"usage_event_id": str(usage_event_id)},
                )

            now = datetime.now(timezone.utc)
            hold.remaining_microtickets = 0
            hold.status = "settled"
            hold.settled_at = now
            AITaskStateMachine.ensure_task_transition(task.status, "support_review")
            task.status = "support_review"
            task.provider_billable = True
            task.support_review_reason = normalized_review_reason
            task.safe_error_code = safe_error_code
            task.safe_error_message = safe_error_message
            task.result = None
            task.completed_at = now
            if attempt is not None:
                AITaskStateMachine.ensure_attempt_transition(
                    attempt.status,
                    "failed",
                )
                attempt.status = "failed"
                attempt.raw_usage = normalized_raw_usage
                attempt.completed_at = now
            session.flush()
            return FailureSettlement(
                task_id=str(task.id),
                attempt_id=str(attempt.id) if attempt is not None else None,
                usage_event_id=str(usage_event_id),
                outcome="billable_failure",
                metering_tokens=metering_tokens,
                charged_microtickets=charged_microtickets,
                released_microtickets=released_microtickets,
                wallet=wallet_snapshot,
                support_review=True,
            )
