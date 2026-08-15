from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import UserContext
from .credentials import reject_plaintext_secrets
from .database import Database
from .db_models import TicketLedgerRecord, TicketWalletRecord
from .observability import events, metrics
from .identifiers import parse_database_id, parse_optional_database_id
from .ticket_math import (
    add_microtickets,
    subtract_microtickets,
    validate_microtickets,
)


CreditEntryType = Literal["grant", "adjustment", "compensation"]


class TicketWalletNotFoundError(LookupError):
    pass


class TicketWalletConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TicketWalletSnapshot:
    user_id: str
    available_microtickets: int
    held_microtickets: int
    lifetime_granted_microtickets: int
    lifetime_spent_microtickets: int
    version: int

    @property
    def total_microtickets(self) -> int:
        return add_microtickets(
            self.available_microtickets,
            self.held_microtickets,
        )


class TicketWalletService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _id(value: str | int | None, name: str) -> int | None:
        try:
            return parse_optional_database_id(value, field=name)
        except ValueError as exc:
            raise TicketWalletConflictError(f"{name}无效") from exc

    @classmethod
    def _required_id(cls, value: str | int, name: str) -> int:
        canonical = cls._id(value, name)
        if canonical is None:
            raise TicketWalletConflictError(f"{name}无效")
        return canonical

    @staticmethod
    def _reason(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise TicketWalletConflictError("账务变更必须填写原因")
        if len(normalized) > 2000:
            raise TicketWalletConflictError("账务变更原因不能超过 2000 个字符")
        return normalized

    @staticmethod
    def _correlation(value: Mapping[str, object] | None) -> dict[str, object]:
        copied = copy.deepcopy(dict(value or {}))
        try:
            json.dumps(copied, allow_nan=False)
            reject_plaintext_secrets(copied, path="账务关联信息")
        except (TypeError, ValueError) as exc:
            message = str(exc) or "账务关联信息必须是 JSON 数据"
            raise TicketWalletConflictError(message) from exc
        return copied

    @staticmethod
    def _snapshot(wallet: TicketWalletRecord) -> TicketWalletSnapshot:
        TicketWalletService._assert_invariants(wallet)
        return TicketWalletSnapshot(
            user_id=str(wallet.user_id),
            available_microtickets=wallet.available_microtickets,
            held_microtickets=wallet.held_microtickets,
            lifetime_granted_microtickets=wallet.lifetime_granted_microtickets,
            lifetime_spent_microtickets=wallet.lifetime_spent_microtickets,
            version=wallet.version,
        )

    @staticmethod
    def _assert_invariants(wallet: TicketWalletRecord) -> None:
        values = {
            "available_microtickets": wallet.available_microtickets,
            "held_microtickets": wallet.held_microtickets,
            "lifetime_granted_microtickets": wallet.lifetime_granted_microtickets,
            "lifetime_spent_microtickets": wallet.lifetime_spent_microtickets,
        }
        if any(not isinstance(value, int) or value < 0 for value in values.values()):
            metrics.increment(
                "wallet_invariant_failures_total",
                labels={"operation": "validate", "resource": "wallet"},
            )
            events.emit(
                "wallet.invariant_failed",
                user_id=str(wallet.user_id),
                version=wallet.version,
                invalid_fields=[name for name, value in values.items() if not isinstance(value, int) or value < 0],
            )
            raise TicketWalletConflictError("算力券钱包余额不变量已损坏")

    @staticmethod
    def require_wallet_for_update(
        session: Session,
        user_id: str | int,
    ) -> TicketWalletRecord:
        canonical_user_id = TicketWalletService._required_id(user_id, "用户标识")
        wallet = session.scalar(
            select(TicketWalletRecord)
            .where(TicketWalletRecord.user_id == canonical_user_id)
            .with_for_update()
        )
        if wallet is None:
            raise TicketWalletNotFoundError("算力券钱包不存在")
        return wallet

    @classmethod
    def create_wallet_in_session(
        cls,
        session: Session,
        user_id: str | int,
        initial_grant_microtickets: int,
        *,
        reason: str,
        actor_user_id: str | int | None = None,
        correlation: Mapping[str, object] | None = None,
    ) -> TicketWalletSnapshot:
        canonical_user_id = cls._required_id(user_id, "用户标识")
        amount = validate_microtickets(
            initial_grant_microtickets,
            name="初始赠送微算力券",
        )
        normalized_reason = cls._reason(reason)
        normalized_actor_user_id = cls._id(actor_user_id, "操作用户标识")
        normalized_correlation = cls._correlation(correlation)
        wallet = TicketWalletRecord(
            user_id=canonical_user_id,
            available_microtickets=amount,
            held_microtickets=0,
            lifetime_granted_microtickets=amount,
            lifetime_spent_microtickets=0,
            version=1,
        )
        ledger = TicketLedgerRecord(
            user_id=canonical_user_id,
            actor_user_id=normalized_actor_user_id,
            entry_type="grant",
            amount_microtickets=amount,
            available_delta=amount,
            held_delta=0,
            available_after=amount,
            held_after=0,
            reason=normalized_reason,
            correlation=normalized_correlation,
        )
        session.add(wallet)
        session.flush()
        session.add(ledger)
        return cls._snapshot(wallet)

    @classmethod
    def _append_entry(
        cls,
        session: Session,
        wallet: TicketWalletRecord,
        *,
        entry_type: str,
        amount_microtickets: int,
        available_delta: int,
        held_delta: int,
        reason: str,
        workspace_id: str | int | None = None,
        project_id: str | int | None = None,
        task_id: str | int | None = None,
        hold_id: str | int | None = None,
        actor_user_id: str | int | None = None,
        correlation: Mapping[str, object] | None = None,
    ) -> TicketWalletSnapshot:
        amount = validate_microtickets(amount_microtickets)
        normalized_reason = cls._reason(reason)
        normalized_workspace_id = cls._id(workspace_id, "工作区标识")
        normalized_project_id = cls._id(project_id, "项目标识")
        normalized_task_id = cls._id(task_id, "任务标识")
        normalized_hold_id = cls._id(hold_id, "占用标识")
        normalized_actor_user_id = cls._id(actor_user_id, "操作用户标识")
        normalized_correlation = cls._correlation(correlation)
        cls._assert_invariants(wallet)
        try:
            next_available = wallet.available_microtickets
            next_held = wallet.held_microtickets
            if available_delta >= 0:
                next_available = add_microtickets(next_available, available_delta)
            else:
                next_available = subtract_microtickets(next_available, -available_delta)
            if held_delta >= 0:
                next_held = add_microtickets(next_held, held_delta)
            else:
                next_held = subtract_microtickets(next_held, -held_delta)
        except Exception as exc:
            metrics.increment(
                "wallet_invariant_failures_total",
                labels={"operation": entry_type, "resource": "wallet"},
            )
            events.emit(
                "wallet.mutation_rejected",
                user_id=str(wallet.user_id),
                operation=entry_type,
                error_type=type(exc).__name__,
            )
            raise

        wallet.available_microtickets = next_available
        wallet.held_microtickets = next_held
        wallet.version += 1
        cls._assert_invariants(wallet)
        session.add(
            TicketLedgerRecord(
                user_id=wallet.user_id,
                workspace_id=normalized_workspace_id,
                project_id=normalized_project_id,
                task_id=normalized_task_id,
                hold_id=normalized_hold_id,
                actor_user_id=normalized_actor_user_id,
                entry_type=entry_type,
                amount_microtickets=amount,
                available_delta=available_delta,
                held_delta=held_delta,
                available_after=next_available,
                held_after=next_held,
                reason=normalized_reason,
                correlation=normalized_correlation,
            )
        )
        return cls._snapshot(wallet)

    @classmethod
    def credit_in_session(
        cls,
        session: Session,
        wallet: TicketWalletRecord,
        amount_microtickets: int,
        *,
        entry_type: CreditEntryType,
        reason: str,
        actor_user_id: str | int | None = None,
        correlation: Mapping[str, object] | None = None,
    ) -> TicketWalletSnapshot:
        if entry_type not in {"grant", "adjustment", "compensation"}:
            raise TicketWalletConflictError("入账流水类型无效")
        amount = validate_microtickets(amount_microtickets)
        next_lifetime_granted = add_microtickets(
            wallet.lifetime_granted_microtickets,
            amount,
        )
        cls._append_entry(
            session,
            wallet,
            entry_type=entry_type,
            amount_microtickets=amount,
            available_delta=amount,
            held_delta=0,
            reason=reason,
            actor_user_id=actor_user_id,
            correlation=correlation,
        )
        wallet.lifetime_granted_microtickets = next_lifetime_granted
        return cls._snapshot(wallet)

    @classmethod
    def debit_adjustment_in_session(
        cls,
        session: Session,
        wallet: TicketWalletRecord,
        amount_microtickets: int,
        *,
        reason: str,
        actor_user_id: str | int,
        correlation: Mapping[str, object] | None = None,
    ) -> TicketWalletSnapshot:
        amount = validate_microtickets(amount_microtickets)
        return cls._append_entry(
            session,
            wallet,
            entry_type="adjustment",
            amount_microtickets=amount,
            available_delta=-amount,
            held_delta=0,
            reason=reason,
            actor_user_id=actor_user_id,
            correlation=correlation,
        )

    @classmethod
    def hold_in_session(
        cls,
        session: Session,
        wallet: TicketWalletRecord,
        amount_microtickets: int,
        *,
        reason: str,
        workspace_id: str | int | None = None,
        project_id: str | int | None = None,
        task_id: str | int | None = None,
        hold_id: str | int | None = None,
        correlation: Mapping[str, object] | None = None,
    ) -> TicketWalletSnapshot:
        amount = validate_microtickets(amount_microtickets)
        return cls._append_entry(
            session,
            wallet,
            entry_type="hold",
            amount_microtickets=amount,
            available_delta=-amount,
            held_delta=amount,
            reason=reason,
            workspace_id=workspace_id,
            project_id=project_id,
            task_id=task_id,
            hold_id=hold_id,
            correlation=correlation,
        )

    @classmethod
    def settle_in_session(
        cls,
        session: Session,
        wallet: TicketWalletRecord,
        amount_microtickets: int,
        *,
        reason: str,
        workspace_id: str | int,
        project_id: str | int | None,
        task_id: str | int,
        hold_id: str | int,
        correlation: Mapping[str, object] | None = None,
    ) -> TicketWalletSnapshot:
        amount = validate_microtickets(amount_microtickets)
        next_lifetime_spent = add_microtickets(
            wallet.lifetime_spent_microtickets,
            amount,
        )
        cls._append_entry(
            session,
            wallet,
            entry_type="settlement",
            amount_microtickets=amount,
            available_delta=0,
            held_delta=-amount,
            reason=reason,
            workspace_id=workspace_id,
            project_id=project_id,
            task_id=task_id,
            hold_id=hold_id,
            correlation=correlation,
        )
        wallet.lifetime_spent_microtickets = next_lifetime_spent
        return cls._snapshot(wallet)

    @classmethod
    def release_in_session(
        cls,
        session: Session,
        wallet: TicketWalletRecord,
        amount_microtickets: int,
        *,
        reason: str,
        workspace_id: str | int,
        project_id: str | int | None,
        task_id: str | int,
        hold_id: str | int,
        correlation: Mapping[str, object] | None = None,
    ) -> TicketWalletSnapshot:
        amount = validate_microtickets(amount_microtickets)
        return cls._append_entry(
            session,
            wallet,
            entry_type="release",
            amount_microtickets=amount,
            available_delta=amount,
            held_delta=-amount,
            reason=reason,
            workspace_id=workspace_id,
            project_id=project_id,
            task_id=task_id,
            hold_id=hold_id,
            correlation=correlation,
        )

    def get_wallet(self, identity: UserContext) -> TicketWalletSnapshot:
        with self.database.transaction(identity) as session:
            wallet = session.scalar(
                select(TicketWalletRecord).where(
                    TicketWalletRecord.user_id
                    == parse_database_id(identity.user_id, field="用户标识")
                )
            )
            if wallet is None:
                raise TicketWalletNotFoundError("算力券钱包不存在")
            return self._snapshot(wallet)
