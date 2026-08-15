from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts import WorkspaceContext
from .database import Database
from .db_models import (
    AITaskRecord,
    ProjectRecord,
    TicketHoldRecord,
    TicketWalletRecord,
    WorkspaceRecord,
)
from .identifiers import parse_database_id, parse_optional_database_id
from .ticket_math import TicketArithmeticError, metering_tokens_to_microtickets
from .ticket_wallet import TicketWalletService, TicketWalletSnapshot


IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class TicketReservationError(RuntimeError):
    pass


class TicketReservationConflictError(TicketReservationError):
    pass


class TicketReservationScopeNotFoundError(TicketReservationError):
    pass


class InsufficientTicketBalanceError(TicketReservationError):
    def __init__(self, available_microtickets: int, required_microtickets: int) -> None:
        super().__init__("可用算力券不足")
        self.available_microtickets = available_microtickets
        self.required_microtickets = required_microtickets


class AIConcurrencyLimitError(TicketReservationError):
    def __init__(self, maximum_concurrency: int) -> None:
        super().__init__("当前 AI 任务已达到并发上限，请等待任务完成后重试")
        self.maximum_concurrency = maximum_concurrency


@dataclass(frozen=True, slots=True)
class ReservedTask:
    task_id: str
    hold_id: str
    status: str
    capability: str
    quoted_microtickets: int
    tokens_per_ticket: int
    request_fingerprint: str
    wallet: TicketWalletSnapshot
    reused: bool = False


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
        raise TicketReservationConflictError(f"{name}必须是有效的 JSON 对象") from exc
    if not isinstance(decoded, dict):
        raise TicketReservationConflictError(f"{name}必须是有效的 JSON 对象")
    return decoded


def request_fingerprint(
    context: WorkspaceContext,
    *,
    capability: str,
    project_id: str | None,
    request_payload: Mapping[str, Any],
) -> str:
    fingerprint_payload = {
        "capability": capability,
        "project_id": project_id,
        "request": _json_object(request_payload, "AI 请求"),
        "workspace_id": context.workspace_id,
    }
    encoded = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TicketReservationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _id(value: str | int, name: str) -> int:
        try:
            return parse_database_id(value, field=name)
        except ValueError as exc:
            raise TicketReservationConflictError(f"{name}无效") from exc

    @staticmethod
    def _optional_id(value: str | int | None, name: str) -> int | None:
        try:
            return parse_optional_database_id(value, field=name)
        except ValueError as exc:
            raise TicketReservationConflictError(f"{name}无效") from exc

    @staticmethod
    def _idempotency_key(value: str) -> str:
        if not isinstance(value, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
            raise TicketReservationConflictError("幂等键格式无效")
        return value

    @staticmethod
    def _capability(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            raise TicketReservationConflictError("AI 能力标识无效")
        return value.strip()

    @staticmethod
    def _find_existing(
        session: Session,
        *,
        user_id: int,
        idempotency_key: str,
    ) -> AITaskRecord | None:
        return session.scalar(
            select(AITaskRecord).where(
                AITaskRecord.user_id == user_id,
                AITaskRecord.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _existing_result(
        session: Session,
        task: AITaskRecord,
        fingerprint: str,
    ) -> ReservedTask:
        if task.request_fingerprint != fingerprint:
            raise TicketReservationConflictError("幂等键已用于不同的 AI 请求")
        hold = session.scalar(
            select(TicketHoldRecord).where(
                TicketHoldRecord.user_id == task.user_id,
                TicketHoldRecord.task_id == task.id,
            ).order_by(TicketHoldRecord.created_at, TicketHoldRecord.id).limit(1)
        )
        if hold is None:
            raise TicketReservationConflictError("原 AI 任务缺少算力券预扣记录")
        wallet = session.scalar(
            select(TicketWalletRecord).where(
                TicketWalletRecord.user_id == task.user_id
            )
        )
        if wallet is None:
            raise TicketReservationConflictError("原 AI 任务缺少算力券钱包")
        return ReservedTask(
            task_id=str(task.id),
            hold_id=str(hold.id),
            status=task.status,
            capability=task.capability,
            quoted_microtickets=task.quoted_microtickets,
            tokens_per_ticket=task.tokens_per_ticket,
            request_fingerprint=task.request_fingerprint,
            wallet=TicketWalletService._snapshot(wallet),
            reused=True,
        )

    def find_existing(
        self,
        context: WorkspaceContext,
        *,
        capability: str,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        project_id: str | None = None,
    ) -> ReservedTask | None:
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        canonical_project_id = self._optional_id(project_id, "项目标识")
        canonical_capability = self._capability(capability)
        canonical_key = self._idempotency_key(idempotency_key)
        normalized_request = _json_object(request_payload, "AI 请求")
        fingerprint = request_fingerprint(
            WorkspaceContext(
                identity=context.identity,
                workspace_id=str(workspace_id),
            ),
            capability=canonical_capability,
            project_id=str(canonical_project_id) if canonical_project_id else None,
            request_payload=normalized_request,
        )

        with self.database.transaction(context.identity) as session:
            existing = self._find_existing(
                session,
                user_id=user_id,
                idempotency_key=canonical_key,
            )
            if existing is None:
                return None
            return self._existing_result(session, existing, fingerprint)

    @staticmethod
    def _validate_scope(
        session: Session,
        *,
        user_id: int,
        workspace_id: int,
        project_id: int | None,
    ) -> None:
        workspace_exists = session.scalar(
            select(WorkspaceRecord.id).where(
                WorkspaceRecord.id == workspace_id,
                WorkspaceRecord.user_id == user_id,
                WorkspaceRecord.deleted_at.is_(None),
            )
        )
        if workspace_exists is None:
            raise TicketReservationScopeNotFoundError("工作区不存在")
        if project_id is None:
            return
        project_exists = session.scalar(
            select(ProjectRecord.id).where(
                ProjectRecord.id == project_id,
                ProjectRecord.user_id == user_id,
                ProjectRecord.workspace_id == workspace_id,
                ProjectRecord.deleted_at.is_(None),
            )
        )
        if project_exists is None:
            raise TicketReservationScopeNotFoundError("项目不存在")

    def reserve_task(
        self,
        context: WorkspaceContext,
        *,
        capability: str,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        config_snapshot: Mapping[str, Any],
        maximum_metering_tokens: int,
        tokens_per_ticket: int,
        max_ai_concurrency_per_user: int | None = None,
        project_id: str | None = None,
        validate_resources: Callable[[Session], None] | None = None,
    ) -> ReservedTask:
        user_id = self._id(context.identity.user_id, "用户标识")
        workspace_id = self._id(context.workspace_id, "工作区标识")
        canonical_project_id = self._optional_id(project_id, "项目标识")
        canonical_capability = self._capability(capability)
        canonical_key = self._idempotency_key(idempotency_key)
        normalized_request = _json_object(request_payload, "AI 请求")
        fingerprint = request_fingerprint(
            WorkspaceContext(
                identity=context.identity,
                workspace_id=str(workspace_id),
            ),
            capability=canonical_capability,
            project_id=str(canonical_project_id) if canonical_project_id else None,
            request_payload=normalized_request,
        )

        with self.database.transaction(context.identity) as session:
            existing = self._find_existing(
                session,
                user_id=user_id,
                idempotency_key=canonical_key,
            )
            if existing is not None:
                return self._existing_result(session, existing, fingerprint)

            normalized_config = _json_object(config_snapshot, "任务配置快照")
            try:
                quoted_microtickets = metering_tokens_to_microtickets(
                    maximum_metering_tokens,
                    tokens_per_ticket,
                )
            except TicketArithmeticError as exc:
                raise TicketReservationConflictError(str(exc)) from exc
            self._validate_scope(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=canonical_project_id,
            )
            if validate_resources is not None:
                validate_resources(session)
            wallet = TicketWalletService.require_wallet_for_update(session, user_id)

            existing = self._find_existing(
                session,
                user_id=user_id,
                idempotency_key=canonical_key,
            )
            if existing is not None:
                return self._existing_result(session, existing, fingerprint)
            if max_ai_concurrency_per_user is not None:
                if (
                    not isinstance(max_ai_concurrency_per_user, int)
                    or isinstance(max_ai_concurrency_per_user, bool)
                    or max_ai_concurrency_per_user < 1
                    or max_ai_concurrency_per_user > 100
                ):
                    raise TicketReservationConflictError("单用户 AI 并发上限无效")
                active_count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(AITaskRecord)
                        .where(
                            AITaskRecord.user_id == user_id,
                            AITaskRecord.status.in_(
                                {"reserved", "queued", "running", "provider_succeeded"}
                            ),
                        )
                    )
                    or 0
                )
                if active_count >= max_ai_concurrency_per_user:
                    raise AIConcurrencyLimitError(max_ai_concurrency_per_user)
            if wallet.available_microtickets < quoted_microtickets:
                raise InsufficientTicketBalanceError(
                    wallet.available_microtickets,
                    quoted_microtickets,
                )

            task = AITaskRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=canonical_project_id,
                capability=canonical_capability,
                status="reserved",
                idempotency_key=canonical_key,
                request_fingerprint=fingerprint,
                request_payload=normalized_request,
                config_snapshot=normalized_config,
                tokens_per_ticket=tokens_per_ticket,
                quoted_microtickets=quoted_microtickets,
                provider_billable=False,
            )
            session.add(task)
            session.flush()
            hold = TicketHoldRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=task.id,
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
                reason="AI 任务额度预扣",
                workspace_id=workspace_id,
                project_id=canonical_project_id,
                task_id=task.id,
                hold_id=hold.id,
                correlation={
                    "capability": canonical_capability,
                    "idempotency_key": canonical_key,
                    "maximum_metering_tokens": maximum_metering_tokens,
                    "tokens_per_ticket": tokens_per_ticket,
                },
            )
            session.flush()
            return ReservedTask(
                task_id=str(task.id),
                hold_id=str(hold.id),
                status=task.status,
                capability=canonical_capability,
                quoted_microtickets=quoted_microtickets,
                tokens_per_ticket=tokens_per_ticket,
                request_fingerprint=fingerprint,
                wallet=wallet_snapshot,
            )
