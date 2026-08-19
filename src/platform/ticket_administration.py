from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select

from .admin_access import require_platform_admin_context
from .contracts import AdminContext
from .database import Database
from .db_models import (
    AuditEventRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UserRecord,
)
from .ticket_math import TicketArithmeticError, validate_microtickets
from .ticket_wallet import TicketWalletService, TicketWalletSnapshot
from .identifiers import parse_database_id


TicketAdjustmentOperation = Literal["grant", "debit", "compensation"]


class TicketAdministrationError(RuntimeError):
    pass


class TicketAdministrationNotFoundError(TicketAdministrationError):
    pass


class TicketAdministrationConflictError(TicketAdministrationError):
    pass


@dataclass(frozen=True, slots=True)
class TicketLedgerItem:
    id: str
    entry_type: str
    amount_microtickets: int
    available_delta: int
    held_delta: int
    available_after: int
    held_after: int
    reason: str | None
    actor_admin_id: str | None
    legacy_actor_user_id: str | None
    correlation: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminTicketWalletView:
    wallet: TicketWalletSnapshot
    ledger: tuple[TicketLedgerItem, ...]
    total_ledger_entries: int


class TicketAdministrationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _require_admin(admin: AdminContext) -> int:
        try:
            require_platform_admin_context(admin)
            return parse_database_id(admin.admin_id, field="管理员 ID")
        except (PermissionError, ValueError) as exc:
            raise TicketAdministrationConflictError("管理员标识无效") from exc

    @staticmethod
    def _reason(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise TicketAdministrationConflictError("算力券调整必须填写原因")
        normalized = value.strip()
        if len(normalized) > 2000:
            raise TicketAdministrationConflictError(
                "算力券调整原因不能超过 2000 个字符"
            )
        return normalized

    @staticmethod
    def _correlation_id(value: str | None) -> str:
        normalized = (value or "").strip() or str(uuid.uuid4())
        if len(normalized) > 80:
            raise TicketAdministrationConflictError("请求关联标识过长")
        return normalized

    @staticmethod
    def _ledger_item(record: TicketLedgerRecord) -> TicketLedgerItem:
        return TicketLedgerItem(
            id=str(record.id),
            entry_type=record.entry_type,
            amount_microtickets=record.amount_microtickets,
            available_delta=record.available_delta,
            held_delta=record.held_delta,
            available_after=record.available_after,
            held_after=record.held_after,
            reason=record.reason,
            actor_admin_id=(
                str(record.actor_admin_id) if record.actor_admin_id is not None else None
            ),
            legacy_actor_user_id=(
                str(record.actor_user_id) if record.actor_user_id is not None else None
            ),
            correlation=dict(record.correlation or {}),
            created_at=record.created_at,
        )

    def adjust(
        self,
        admin: AdminContext,
        target_user_id: int,
        *,
        operation: TicketAdjustmentOperation,
        amount_microtickets: int,
        reason: str,
        correlation_id: str | None = None,
    ) -> TicketWalletSnapshot:
        actor_admin_id = self._require_admin(admin)
        if operation not in {"grant", "debit", "compensation"}:
            raise TicketAdministrationConflictError("算力券调整类型无效")
        try:
            amount = validate_microtickets(amount_microtickets)
        except TicketArithmeticError as exc:
            raise TicketAdministrationConflictError(str(exc)) from exc
        if amount == 0:
            raise TicketAdministrationConflictError("算力券调整数量必须大于零")
        normalized_reason = self._reason(reason)
        normalized_correlation_id = self._correlation_id(correlation_id)

        with self.database.transaction(admin) as session:
            if session.get(UserRecord, target_user_id) is None:
                raise TicketAdministrationNotFoundError("用户不存在")
            wallet = TicketWalletService.require_wallet_for_update(
                session,
                target_user_id,
            )
            before = TicketWalletService._snapshot(wallet)
            try:
                if operation == "debit":
                    after = TicketWalletService.debit_adjustment_in_session(
                        session,
                        wallet,
                        amount,
                        reason=normalized_reason,
                        actor_admin_id=actor_admin_id,
                        correlation={
                            "admin_operation": operation,
                            "correlation_id": normalized_correlation_id,
                        },
                    )
                else:
                    after = TicketWalletService.credit_in_session(
                        session,
                        wallet,
                        amount,
                        entry_type=operation,
                        reason=normalized_reason,
                        actor_admin_id=actor_admin_id,
                        correlation={
                            "admin_operation": operation,
                            "correlation_id": normalized_correlation_id,
                        },
                    )
            except TicketArithmeticError as exc:
                raise TicketAdministrationConflictError(str(exc)) from exc

            session.add(
                AuditEventRecord(
                    actor_admin_id=actor_admin_id,
                    target_user_id=target_user_id,
                    action=f"ticket.{operation}",
                    target_type="ticket_wallet",
                    target_id=str(target_user_id),
                    reason=normalized_reason,
                    before_summary={
                        "available_microtickets": before.available_microtickets,
                        "held_microtickets": before.held_microtickets,
                    },
                    after_summary={
                        "amount_microtickets": amount,
                        "available_microtickets": after.available_microtickets,
                        "held_microtickets": after.held_microtickets,
                    },
                    correlation_id=normalized_correlation_id,
                )
            )
            session.flush()
            return after

    def get_wallet(
        self,
        admin: AdminContext,
        target_user_id: int,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> AdminTicketWalletView:
        self._require_admin(admin)
        if offset < 0 or limit < 1 or limit > 100:
            raise TicketAdministrationConflictError("流水分页参数无效")
        with self.database.transaction(admin) as session:
            if session.get(UserRecord, target_user_id) is None:
                raise TicketAdministrationNotFoundError("用户不存在")
            wallet = session.scalar(
                select(TicketWalletRecord).where(
                    TicketWalletRecord.user_id == target_user_id
                )
            )
            if wallet is None:
                raise TicketAdministrationNotFoundError("算力券钱包不存在")
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(TicketLedgerRecord)
                    .where(TicketLedgerRecord.user_id == target_user_id)
                )
                or 0
            )
            records = session.scalars(
                select(TicketLedgerRecord)
                .where(TicketLedgerRecord.user_id == target_user_id)
                .order_by(TicketLedgerRecord.created_at.desc(), TicketLedgerRecord.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return AdminTicketWalletView(
                wallet=TicketWalletService._snapshot(wallet),
                ledger=tuple(self._ledger_item(record) for record in records),
                total_ledger_entries=total,
            )
