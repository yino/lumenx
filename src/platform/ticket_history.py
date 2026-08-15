from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select

from .contracts import UserContext
from .database import Database
from .db_models import (
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
)
from .ticket_wallet import TicketWalletService, TicketWalletSnapshot
from .identifiers import parse_database_id, parse_optional_database_id


class TicketHistoryNotFoundError(LookupError):
    pass


class TicketHistoryConflictError(ValueError):
    pass


HistoryView = Literal["ledger", "usage"]


@dataclass(frozen=True, slots=True)
class UserLedgerHistoryItem:
    id: str
    entry_type: str
    operation_zh: str
    workspace_id: str | None
    project_id: str | None
    task_id: str | None
    metering_tokens: int | None
    amount_microtickets: int
    display_delta_microtickets: int
    available_after: int
    held_after: int
    status: str
    status_zh: str
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UserUsageHistoryItem:
    id: str
    workspace_id: str
    project_id: str | None
    task_id: str
    capability: str
    outcome: str
    status_zh: str
    metering_tokens: int
    charged_microtickets: int
    tokens_per_ticket: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PaginatedTicketHistory:
    view: HistoryView
    items: tuple[UserLedgerHistoryItem | UserUsageHistoryItem, ...]
    total: int
    offset: int
    limit: int


LEDGER_OPERATION_ZH = {
    "grant": "算力券赠送",
    "hold": "AI 任务预扣",
    "settlement": "AI 任务结算",
    "release": "释放预扣",
    "adjustment": "人工调整",
    "compensation": "异常补偿",
}

USAGE_STATUS_ZH = {
    "succeeded": "已完成",
    "nonbillable_failure": "失败未计费",
    "billable_failure": "计费失败待复核",
    "cancelled": "已取消未计费",
}


class TicketHistoryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _user_id(identity: UserContext) -> int:
        try:
            return parse_database_id(identity.user_id, field="用户 ID")
        except ValueError as exc:
            raise TicketHistoryConflictError("用户标识无效") from exc

    @staticmethod
    def _pagination(offset: int, limit: int) -> None:
        if offset < 0 or limit < 1 or limit > 100:
            raise TicketHistoryConflictError("历史记录分页参数无效")

    @staticmethod
    def _optional_id(value: object) -> int | None:
        try:
            return parse_optional_database_id(value, field="记录 ID")
        except ValueError:
            return None

    @staticmethod
    def _display_delta(record: TicketLedgerRecord) -> int:
        if record.entry_type == "settlement":
            return -record.amount_microtickets
        if record.entry_type == "release":
            return record.amount_microtickets
        return record.available_delta

    @staticmethod
    def _ledger_status(
        record: TicketLedgerRecord,
        hold: TicketHoldRecord | None,
    ) -> tuple[str, str]:
        if record.entry_type == "hold":
            if hold is None or hold.status == "held":
                return "held", "预扣中"
            if hold.status == "settled":
                return "settled", "已结算"
            return "released", "已释放"
        if record.entry_type == "settlement":
            return "settled", "已结算"
        if record.entry_type == "release":
            return "released", "已释放"
        return "posted", "已入账"

    def get_summary(self, identity: UserContext) -> TicketWalletSnapshot:
        user_id = self._user_id(identity)
        with self.database.transaction(identity) as session:
            wallet = session.scalar(
                select(TicketWalletRecord).where(
                    TicketWalletRecord.user_id == user_id
                )
            )
            if wallet is None:
                raise TicketHistoryNotFoundError("算力券钱包不存在")
            return TicketWalletService._snapshot(wallet)

    def list_history(
        self,
        identity: UserContext,
        *,
        view: HistoryView,
        offset: int = 0,
        limit: int = 20,
    ) -> PaginatedTicketHistory:
        user_id = self._user_id(identity)
        self._pagination(offset, limit)
        if view not in {"ledger", "usage"}:
            raise TicketHistoryConflictError("历史记录类型无效")
        with self.database.transaction(identity) as session:
            if session.scalar(
                select(TicketWalletRecord.id).where(
                    TicketWalletRecord.user_id == user_id
                )
            ) is None:
                raise TicketHistoryNotFoundError("算力券钱包不存在")
            if view == "usage":
                total = int(
                    session.scalar(
                        select(func.count())
                        .select_from(UsageEventRecord)
                        .where(UsageEventRecord.user_id == user_id)
                    )
                    or 0
                )
                records = list(
                    session.scalars(
                        select(UsageEventRecord)
                        .where(UsageEventRecord.user_id == user_id)
                        .order_by(
                            UsageEventRecord.created_at.desc(),
                            UsageEventRecord.id.desc(),
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                )
                items = tuple(
                    UserUsageHistoryItem(
                        id=str(record.id),
                        workspace_id=str(record.workspace_id),
                        project_id=(
                            str(record.project_id) if record.project_id is not None else None
                        ),
                        task_id=str(record.task_id),
                        capability=record.capability,
                        outcome=record.outcome,
                        status_zh=USAGE_STATUS_ZH.get(record.outcome, "状态未知"),
                        metering_tokens=record.metering_tokens,
                        charged_microtickets=record.charged_microtickets,
                        tokens_per_ticket=record.tokens_per_ticket,
                        created_at=record.created_at,
                    )
                    for record in records
                )
                return PaginatedTicketHistory(
                    view="usage",
                    items=items,
                    total=total,
                    offset=offset,
                    limit=limit,
                )

            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(TicketLedgerRecord)
                    .where(TicketLedgerRecord.user_id == user_id)
                )
                or 0
            )
            records = list(
                session.scalars(
                    select(TicketLedgerRecord)
                    .where(TicketLedgerRecord.user_id == user_id)
                    .order_by(
                        TicketLedgerRecord.created_at.desc(),
                        TicketLedgerRecord.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            hold_ids = {record.hold_id for record in records if record.hold_id is not None}
            holds = {
                hold.id: hold
                for hold in session.scalars(
                    select(TicketHoldRecord).where(
                        TicketHoldRecord.user_id == user_id,
                        TicketHoldRecord.id.in_(hold_ids),
                    )
                )
            } if hold_ids else {}
            usage_ids = {
                usage_id
                for record in records
                if isinstance(record.correlation, dict)
                if (
                    usage_id := self._optional_id(
                        record.correlation.get("usage_event_id")
                    )
                )
                is not None
            }
            usage_events = {
                usage.id: usage
                for usage in session.scalars(
                    select(UsageEventRecord).where(
                        UsageEventRecord.user_id == user_id,
                        UsageEventRecord.id.in_(usage_ids),
                    )
                )
            } if usage_ids else {}
            ledger_items: list[UserLedgerHistoryItem] = []
            for record in records:
                hold = holds.get(record.hold_id) if record.hold_id is not None else None
                status, status_zh = self._ledger_status(record, hold)
                raw_usage_id = (
                    record.correlation.get("usage_event_id")
                    if isinstance(record.correlation, dict)
                    else None
                )
                usage_id = self._optional_id(raw_usage_id)
                usage = usage_events.get(usage_id) if usage_id is not None else None
                ledger_items.append(
                    UserLedgerHistoryItem(
                        id=str(record.id),
                        entry_type=record.entry_type,
                        operation_zh=LEDGER_OPERATION_ZH.get(
                            record.entry_type,
                            "账务操作",
                        ),
                        workspace_id=(
                            str(record.workspace_id)
                            if record.workspace_id is not None
                            else None
                        ),
                        project_id=(
                            str(record.project_id) if record.project_id is not None else None
                        ),
                        task_id=str(record.task_id) if record.task_id is not None else None,
                        metering_tokens=(usage.metering_tokens if usage is not None else None),
                        amount_microtickets=record.amount_microtickets,
                        display_delta_microtickets=self._display_delta(record),
                        available_after=record.available_after,
                        held_after=record.held_after,
                        status=status,
                        status_zh=status_zh,
                        reason=record.reason,
                        created_at=record.created_at,
                    )
                )
            return PaginatedTicketHistory(
                view="ledger",
                items=tuple(ledger_items),
                total=total,
                offset=offset,
                limit=limit,
            )
