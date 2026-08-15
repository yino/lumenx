from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts import WorkspaceContext
from .database import Database
from .db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    TicketHoldRecord,
    TicketWalletRecord,
)
from .identifiers import parse_database_id
from .ticket_math import TicketArithmeticError, metering_tokens_to_microtickets
from .ticket_reservation import InsufficientTicketBalanceError
from .ticket_wallet import TicketWalletService, TicketWalletSnapshot


RETRY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class RetryAccountingError(RuntimeError):
    pass


class RetryAccountingConflictError(RetryAccountingError):
    pass


class RetryAccountingScopeNotFoundError(RetryAccountingError):
    pass


@dataclass(frozen=True, slots=True)
class RetryReservation:
    task_id: str
    attempt_id: str
    hold_id: str
    attempt_number: int
    quoted_microtickets: int
    wallet: TicketWalletSnapshot
    reused_existing_hold: bool = False
    reused_retry: bool = False
    released_previous_microtickets: int = 0


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
        raise RetryAccountingConflictError(f"{name}必须是有效的 JSON 对象") from exc
    if not isinstance(decoded, dict):
        raise RetryAccountingConflictError(f"{name}必须是有效的 JSON 对象")
    return decoded


class RetryAccountingService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _id(value: str | int, name: str) -> int:
        try:
            return parse_database_id(value, field=name)
        except ValueError as exc:
            raise RetryAccountingConflictError(f"{name}无效") from exc

    @staticmethod
    def _retry_key(value: str) -> str:
        if not isinstance(value, str) or not RETRY_KEY_PATTERN.fullmatch(value):
            raise RetryAccountingConflictError("重试幂等键格式无效")
        return value

    @staticmethod
    def _provider_value(value: str, name: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise RetryAccountingConflictError(f"{name}无效")
        return value.strip()

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
            raise RetryAccountingScopeNotFoundError("AI 任务不存在")
        return task

    @staticmethod
    def _ensure_retryable_task(task: AITaskRecord) -> None:
        if task.status not in {"reserved", "queued", "running"}:
            raise RetryAccountingConflictError("AI 任务当前状态不允许重试")

    @staticmethod
    def _require_attempt_for_update(
        session: Session,
        task: AITaskRecord,
        attempt_id: int,
    ) -> AITaskAttemptRecord:
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
            raise RetryAccountingScopeNotFoundError("AI 任务尝试不存在")
        return attempt

    @staticmethod
    def _hold_for_attempt(
        session: Session,
        task: AITaskRecord,
        attempt: AITaskAttemptRecord,
    ) -> TicketHoldRecord:
        hold = session.scalar(
            select(TicketHoldRecord)
            .where(
                TicketHoldRecord.user_id == task.user_id,
                TicketHoldRecord.workspace_id == task.workspace_id,
                TicketHoldRecord.task_id == task.id,
                TicketHoldRecord.attempt_id == attempt.id,
            )
            .with_for_update()
        )
        if hold is not None:
            return hold
        legacy_holds = list(
            session.scalars(
                select(TicketHoldRecord)
                .where(
                    TicketHoldRecord.user_id == task.user_id,
                    TicketHoldRecord.workspace_id == task.workspace_id,
                    TicketHoldRecord.task_id == task.id,
                    TicketHoldRecord.attempt_id.is_(None),
                )
                .with_for_update()
            )
        )
        if len(legacy_holds) != 1:
            raise RetryAccountingConflictError("AI 任务尝试缺少唯一预扣记录")
        legacy_holds[0].attempt_id = attempt.id
        return legacy_holds[0]

    @staticmethod
    def _is_proven_nonbillable(
        task: AITaskRecord,
        attempt: AITaskAttemptRecord,
    ) -> bool:
        if task.provider_billable or attempt.billable_acknowledged_at is not None:
            return False
        diagnostic = attempt.diagnostic if isinstance(attempt.diagnostic, dict) else {}
        confirmed = diagnostic.get("billing_state") == "not_billable"
        never_submitted = (
            attempt.status == "pending"
            and attempt.provider_request_id is None
            and attempt.provider_task_id is None
        )
        return confirmed or never_submitted

    @staticmethod
    def _retry_fingerprint(task_id: int, previous_attempt_id: int) -> str:
        return hashlib.sha256(
            f"{task_id}:{previous_attempt_id}".encode("ascii")
        ).hexdigest()

    @staticmethod
    def _existing_retry(
        session: Session,
        task: AITaskRecord,
        retry_key: str,
    ) -> AITaskAttemptRecord | None:
        return session.scalar(
            select(AITaskAttemptRecord).where(
                AITaskAttemptRecord.task_id == task.id,
                AITaskAttemptRecord.retry_key == retry_key,
            )
        )

    @staticmethod
    def _result_for_attempt(
        session: Session,
        task: AITaskRecord,
        attempt: AITaskAttemptRecord,
        *,
        reused_retry: bool,
    ) -> RetryReservation:
        hold = session.scalar(
            select(TicketHoldRecord).where(
                TicketHoldRecord.user_id == task.user_id,
                TicketHoldRecord.task_id == task.id,
                TicketHoldRecord.attempt_id == attempt.id,
            )
        )
        wallet = session.scalar(
            select(TicketWalletRecord).where(
                TicketWalletRecord.user_id == task.user_id
            )
        )
        if hold is None or wallet is None:
            raise RetryAccountingConflictError("重试尝试缺少预扣或钱包记录")
        return RetryReservation(
            task_id=str(task.id),
            attempt_id=str(attempt.id),
            hold_id=str(hold.id),
            attempt_number=attempt.attempt_number,
            quoted_microtickets=hold.quoted_microtickets,
            wallet=TicketWalletService._snapshot(wallet),
            reused_retry=reused_retry,
        )

    def reuse_proven_nonbillable_hold(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        attempt_id: str,
    ) -> RetryReservation:
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        canonical_task_id = self._id(task_id, "任务标识")
        canonical_attempt_id = self._id(attempt_id, "尝试标识")

        with self.database.transaction(context.identity) as session:
            task = self._require_task_for_update(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            self._ensure_retryable_task(task)
            attempt = self._require_attempt_for_update(
                session,
                task,
                canonical_attempt_id,
            )
            hold = self._hold_for_attempt(session, task, attempt)
            if hold.status != "held":
                raise RetryAccountingConflictError("原尝试预扣已经结束")
            if not self._is_proven_nonbillable(task, attempt):
                raise RetryAccountingConflictError("无法证明原尝试未产生供应商计费")
            wallet = session.scalar(
                select(TicketWalletRecord).where(
                    TicketWalletRecord.user_id == user_id
                )
            )
            if wallet is None:
                raise RetryAccountingConflictError("AI 任务缺少算力券钱包")
            session.flush()
            return RetryReservation(
                task_id=str(task.id),
                attempt_id=str(attempt.id),
                hold_id=str(hold.id),
                attempt_number=attempt.attempt_number,
                quoted_microtickets=hold.quoted_microtickets,
                wallet=TicketWalletService._snapshot(wallet),
                reused_existing_hold=True,
            )

    def reserve_resubmission(
        self,
        context: WorkspaceContext,
        *,
        task_id: str,
        previous_attempt_id: str,
        retry_key: str,
        config_snapshot: Mapping[str, Any],
        provider: str,
        provider_model_id: str,
        maximum_metering_tokens: int,
    ) -> RetryReservation:
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        canonical_task_id = self._id(task_id, "任务标识")
        canonical_previous_attempt_id = self._id(
            previous_attempt_id,
            "原尝试标识",
        )
        canonical_retry_key = self._retry_key(retry_key)
        retry_fingerprint = self._retry_fingerprint(
            canonical_task_id,
            canonical_previous_attempt_id,
        )

        with self.database.transaction(context.identity) as session:
            task = self._require_task_for_update(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=canonical_task_id,
            )
            existing = self._existing_retry(session, task, canonical_retry_key)
            if existing is not None:
                diagnostic = (
                    existing.diagnostic if isinstance(existing.diagnostic, dict) else {}
                )
                if diagnostic.get("retry_fingerprint") != retry_fingerprint:
                    raise RetryAccountingConflictError("重试幂等键已用于不同尝试")
                return self._result_for_attempt(
                    session,
                    task,
                    existing,
                    reused_retry=True,
                )

            self._ensure_retryable_task(task)
            previous_attempt = self._require_attempt_for_update(
                session,
                task,
                canonical_previous_attempt_id,
            )
            if previous_attempt.status not in {"failed", "cancelled", "ambiguous"}:
                raise RetryAccountingConflictError("原尝试当前状态不能重新提交")
            previous_hold = self._hold_for_attempt(session, task, previous_attempt)
            proven_nonbillable = self._is_proven_nonbillable(task, previous_attempt)

            normalized_config = _json_object(config_snapshot, "重试配置快照")
            canonical_provider = self._provider_value(provider, "供应商", 80)
            canonical_model_id = self._provider_value(
                provider_model_id,
                "供应商模型标识",
                160,
            )
            try:
                quoted_microtickets = metering_tokens_to_microtickets(
                    maximum_metering_tokens,
                    task.tokens_per_ticket,
                )
            except TicketArithmeticError as exc:
                raise RetryAccountingConflictError(str(exc)) from exc

            wallet = TicketWalletService.require_wallet_for_update(session, user_id)
            released_previous = 0
            wallet_snapshot = TicketWalletService._snapshot(wallet)
            if proven_nonbillable and previous_hold.status == "held":
                released_previous = previous_hold.remaining_microtickets
                wallet_snapshot = TicketWalletService.release_in_session(
                    session,
                    wallet,
                    released_previous,
                    reason="释放已确认未计费的重试额度",
                    workspace_id=workspace_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    hold_id=previous_hold.id,
                    correlation={"attempt_id": str(previous_attempt.id)},
                )
                previous_hold.remaining_microtickets = 0
                previous_hold.status = "released"
                previous_hold.released_at = datetime.now(timezone.utc)
            elif previous_hold.status != "held":
                raise RetryAccountingConflictError("原尝试预扣状态不允许重新提交")

            if wallet.available_microtickets < quoted_microtickets:
                raise InsufficientTicketBalanceError(
                    wallet.available_microtickets,
                    quoted_microtickets,
                )

            attempt_number = int(
                session.scalar(
                    select(func.max(AITaskAttemptRecord.attempt_number)).where(
                        AITaskAttemptRecord.task_id == task.id
                    )
                )
                or 0
            ) + 1
            attempt = AITaskAttemptRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=task.id,
                retry_of_attempt_id=previous_attempt.id,
                retry_key=canonical_retry_key,
                attempt_number=attempt_number,
                status="pending",
                config_snapshot=normalized_config,
                provider=canonical_provider,
                provider_model_id=canonical_model_id,
                diagnostic={
                    "retry_fingerprint": retry_fingerprint,
                    "retry_of_attempt_id": str(previous_attempt.id),
                },
            )
            session.add(attempt)
            session.flush()
            hold = TicketHoldRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=task.id,
                attempt_id=attempt.id,
                quoted_microtickets=quoted_microtickets,
                remaining_microtickets=quoted_microtickets,
                status="held",
            )
            session.add(hold)
            session.flush()
            wallet_snapshot = TicketWalletService.hold_in_session(
                session,
                wallet,
                quoted_microtickets,
                reason="AI 重试单独预扣",
                workspace_id=workspace_id,
                project_id=task.project_id,
                task_id=task.id,
                hold_id=hold.id,
                correlation={
                    "attempt_id": str(attempt.id),
                    "retry_key": canonical_retry_key,
                    "retry_of_attempt_id": str(previous_attempt.id),
                },
            )
            session.flush()
            return RetryReservation(
                task_id=str(task.id),
                attempt_id=str(attempt.id),
                hold_id=str(hold.id),
                attempt_number=attempt_number,
                quoted_microtickets=quoted_microtickets,
                wallet=wallet_snapshot,
                released_previous_microtickets=released_previous,
            )
