from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from .admin_access import require_platform_admin_context
from .admin_contracts import (
    ManualRechargeOrderStatus,
    normalize_chinese_reason,
)
from .contracts import AdminContext
from .db_models import (
    AuditEventRecord,
    ManualRechargeOrderEventRecord,
    ManualRechargeOrderRecord,
    ManualRechargeReconciliationReportRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UserRecord,
)
from .identifiers import parse_database_id


class ManualRechargeNotFoundError(LookupError):
    pass


class ManualRechargeConflictError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MANUAL_RECHARGE_CONFLICT",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class ManualRechargeOrderPage:
    items: list[ManualRechargeOrderRecord]
    total: int
    total_cash_fen: int
    total_ticket_microtickets: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class ManualRechargeOrderDetail:
    order: ManualRechargeOrderRecord
    events: list[ManualRechargeOrderEventRecord]
    ledger: list[TicketLedgerRecord]
    available_microtickets: int
    held_microtickets: int


@dataclass(frozen=True, slots=True)
class ManualRechargeReconciliationResult:
    report_id: int
    order_id: int
    status: str
    severity: str
    details: dict[str, Any]


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _order_number(now: datetime | None = None) -> str:
    issued_at = now or datetime.now(UTC)
    return f"MR{issued_at:%Y%m%d%H%M%S}{secrets.token_hex(6).upper()}"


def _safe_snapshot(order: ManualRechargeOrderRecord) -> dict[str, Any]:
    return {
        "order_number": order.order_number,
        "user_id": order.user_id,
        "cash_amount_fen": order.cash_amount_fen,
        "ticket_amount_microtickets": order.ticket_amount_microtickets,
        "currency": order.currency,
        "status": order.status,
        "refunded_cash_fen": order.refunded_cash_fen,
        "refunded_microtickets": order.refunded_microtickets,
        "version": order.version,
    }


class ManualRechargeOrderService:
    MAX_CASH_AMOUNT_FEN = 9_999_999_999_999
    MAX_TICKET_AMOUNT_MICROTICKETS = 9_999_999_999_999_999

    def __init__(self, database: Any) -> None:
        self.database = database

    @staticmethod
    def _actor_id(admin: AdminContext) -> int:
        require_platform_admin_context(admin)
        return parse_database_id(admin.admin_id, field="管理员 ID")

    @staticmethod
    def _require_positive(value: int, label: str, *, maximum: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ManualRechargeConflictError(f"{label}必须是正整数", code="AMOUNT_INVALID")
        if value > maximum:
            raise ManualRechargeConflictError(
                f"{label}超过允许上限",
                code="AMOUNT_LIMIT_EXCEEDED",
            )
        return value

    @staticmethod
    def _require_idempotency_key(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 160:
            raise ManualRechargeConflictError(
                "幂等键不能为空且不能超过 160 个字符",
                code="IDEMPOTENCY_KEY_INVALID",
            )
        return normalized

    @staticmethod
    def _reason(value: str, *, field_name: str = "操作原因") -> str:
        try:
            return normalize_chinese_reason(value, field_name=field_name)
        except ValueError as exc:
            raise ManualRechargeConflictError(str(exc), code="REASON_INVALID") from exc

    @staticmethod
    def _locked_order(session: Any, order_id: int) -> ManualRechargeOrderRecord:
        order = session.scalar(
            select(ManualRechargeOrderRecord)
            .where(ManualRechargeOrderRecord.id == order_id)
            .with_for_update()
        )
        if order is None:
            raise ManualRechargeNotFoundError("人工充值订单不存在")
        return order

    @staticmethod
    def _locked_wallet(session: Any, user_id: int) -> TicketWalletRecord:
        wallet = session.scalar(
            select(TicketWalletRecord)
            .where(TicketWalletRecord.user_id == user_id)
            .with_for_update()
        )
        if wallet is None:
            raise ManualRechargeConflictError(
                "目标用户的算力券钱包不存在",
                code="WALLET_NOT_FOUND",
            )
        return wallet

    @staticmethod
    def _replayed_event(
        session: Any,
        *,
        actor_admin_id: int,
        event_type: str,
        idempotency_key: str,
        request_fingerprint: str,
        order_id: int,
    ) -> ManualRechargeOrderRecord | None:
        event = session.scalar(
            select(ManualRechargeOrderEventRecord).where(
                ManualRechargeOrderEventRecord.actor_admin_id == actor_admin_id,
                ManualRechargeOrderEventRecord.event_type == event_type,
                ManualRechargeOrderEventRecord.idempotency_key == idempotency_key,
            )
        )
        if event is None:
            return None
        if event.order_id != order_id or event.request_fingerprint != request_fingerprint:
            raise ManualRechargeConflictError(
                "该幂等键已用于不同的充值订单操作",
                code="IDEMPOTENCY_FINGERPRINT_CONFLICT",
            )
        order = session.get(ManualRechargeOrderRecord, event.order_id)
        if order is None:
            raise ManualRechargeConflictError("幂等记录关联订单缺失", code="ORDER_INTEGRITY_ERROR")
        return order

    @staticmethod
    def _append_audit(
        session: Any,
        *,
        actor_admin_id: int,
        order: ManualRechargeOrderRecord,
        action: str,
        reason: str,
        correlation_id: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
    ) -> None:
        session.add(
            AuditEventRecord(
                actor_admin_id=actor_admin_id,
                target_user_id=order.user_id,
                action=action,
                target_type="manual_recharge_order",
                target_id=str(order.id),
                reason=reason,
                before_summary=before,
                after_summary=after,
                correlation_id=correlation_id,
            )
        )

    def create_pending(
        self,
        admin: AdminContext,
        *,
        user_id: int,
        cash_amount_fen: int,
        ticket_amount_microtickets: int,
        offline_reference: str | None,
        reason: str,
        idempotency_key: str,
        exchange_snapshot: dict[str, Any],
        correlation_id: str | None = None,
    ) -> ManualRechargeOrderRecord:
        actor_admin_id = self._actor_id(admin)
        target_user_id = parse_database_id(user_id, field="用户 ID")
        cash = self._require_positive(
            cash_amount_fen,
            "人民币金额（分）",
            maximum=self.MAX_CASH_AMOUNT_FEN,
        )
        tickets = self._require_positive(
            ticket_amount_microtickets,
            "算力券数量",
            maximum=self.MAX_TICKET_AMOUNT_MICROTICKETS,
        )
        normalized_reason = self._reason(reason)
        key = self._require_idempotency_key(idempotency_key)
        reference = (offline_reference or "").strip() or None
        if reference is not None and len(reference) > 160:
            raise ManualRechargeConflictError("线下凭证编号不能超过 160 个字符")
        request_fingerprint = _fingerprint(
            {
                "action": "create",
                "user_id": target_user_id,
                "cash_amount_fen": cash,
                "ticket_amount_microtickets": tickets,
                "offline_reference": reference,
                "reason": normalized_reason,
                "exchange_snapshot": exchange_snapshot,
            }
        )
        correlation = correlation_id or str(uuid.uuid4())

        try:
            with self.database.transaction(admin) as session:
                replay = session.scalar(
                    select(ManualRechargeOrderRecord).where(
                        ManualRechargeOrderRecord.created_by_admin_id == actor_admin_id,
                        ManualRechargeOrderRecord.create_idempotency_key == key,
                    )
                )
                if replay is not None:
                    if replay.create_request_fingerprint != request_fingerprint:
                        raise ManualRechargeConflictError(
                            "该幂等键已用于不同的充值订单",
                            code="IDEMPOTENCY_FINGERPRINT_CONFLICT",
                        )
                    return replay
                user = session.scalar(
                    select(UserRecord)
                    .where(UserRecord.id == target_user_id)
                    .with_for_update()
                )
                if user is None:
                    raise ManualRechargeNotFoundError("目标用户不存在")
                if user.status != "active":
                    raise ManualRechargeConflictError("停用用户不能创建充值订单")
                self._locked_wallet(session, target_user_id)
                if reference is not None and session.scalar(
                    select(ManualRechargeOrderRecord.id)
                    .where(func.lower(ManualRechargeOrderRecord.offline_reference) == reference.lower())
                    .limit(1)
                ) is not None:
                    raise ManualRechargeConflictError(
                        "该线下凭证编号已存在",
                        code="OFFLINE_REFERENCE_DUPLICATE",
                    )
                order = ManualRechargeOrderRecord(
                    order_number=_order_number(),
                    user_id=target_user_id,
                    cash_amount_fen=cash,
                    ticket_amount_microtickets=tickets,
                    currency="CNY",
                    status="pending",
                    exchange_snapshot=dict(exchange_snapshot),
                    offline_reference=reference,
                    create_reason=normalized_reason,
                    create_idempotency_key=key,
                    create_request_fingerprint=request_fingerprint,
                    version=1,
                    created_by_admin_id=actor_admin_id,
                )
                session.add(order)
                session.flush()
                session.add(
                    ManualRechargeOrderEventRecord(
                        order_id=order.id,
                        user_id=target_user_id,
                        actor_admin_id=actor_admin_id,
                        event_type="created",
                        idempotency_key=key,
                        request_fingerprint=request_fingerprint,
                        cash_amount_fen=cash,
                        ticket_amount_microtickets=tickets,
                        version_before=0,
                        version_after=1,
                        reason=normalized_reason,
                        snapshot=_safe_snapshot(order),
                    )
                )
                self._append_audit(
                    session,
                    actor_admin_id=actor_admin_id,
                    order=order,
                    action="manual_recharge_order.create",
                    reason=normalized_reason,
                    correlation_id=correlation,
                    before=None,
                    after=_safe_snapshot(order),
                )
                session.flush()
                return order
        except IntegrityError as exc:
            with self.database.transaction(admin) as session:
                replay = session.scalar(
                    select(ManualRechargeOrderRecord).where(
                        ManualRechargeOrderRecord.created_by_admin_id == actor_admin_id,
                        ManualRechargeOrderRecord.create_idempotency_key == key,
                    )
                )
                if replay is not None and replay.create_request_fingerprint == request_fingerprint:
                    return replay
            raise ManualRechargeConflictError(
                "订单编号、线下凭证或幂等键已存在",
                code="ORDER_DUPLICATE",
            ) from exc

    def complete(
        self,
        admin: AdminContext,
        order_id: int,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> ManualRechargeOrderRecord:
        actor_admin_id = self._actor_id(admin)
        normalized_reason = self._reason(reason)
        key = self._require_idempotency_key(idempotency_key)
        request_fingerprint = _fingerprint(
            {
                "action": "complete",
                "order_id": order_id,
                "expected_version": expected_version,
                "reason": normalized_reason,
            }
        )
        with self.database.transaction(admin) as session:
            replay = self._replayed_event(
                session,
                actor_admin_id=actor_admin_id,
                event_type="completed",
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                order_id=order_id,
            )
            if replay is not None:
                return replay
            order = self._locked_order(session, order_id)
            replay = self._replayed_event(
                session,
                actor_admin_id=actor_admin_id,
                event_type="completed",
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                order_id=order_id,
            )
            if replay is not None:
                return replay
            if order.version != expected_version:
                raise ManualRechargeConflictError(
                    "订单已被更新，请刷新后重试",
                    code="ORDER_VERSION_CONFLICT",
                    details={"current_version": order.version, "current_status": order.status},
                )
            if order.status != "pending":
                raise ManualRechargeConflictError("只有待确认订单可以完成")
            wallet = self._locked_wallet(session, order.user_id)
            before = _safe_snapshot(order)
            now = datetime.now(UTC)
            order.status = "completed"
            order.completed_by_admin_id = actor_admin_id
            order.completed_at = now
            order.version += 1
            wallet.available_microtickets += order.ticket_amount_microtickets
            wallet.lifetime_recharged_microtickets += order.ticket_amount_microtickets
            wallet.version += 1
            event = ManualRechargeOrderEventRecord(
                order_id=order.id,
                user_id=order.user_id,
                actor_admin_id=actor_admin_id,
                event_type="completed",
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                cash_amount_fen=order.cash_amount_fen,
                ticket_amount_microtickets=order.ticket_amount_microtickets,
                version_before=expected_version,
                version_after=order.version,
                reason=normalized_reason,
                snapshot=_safe_snapshot(order),
            )
            session.add(event)
            session.flush()
            ledger = TicketLedgerRecord(
                user_id=order.user_id,
                actor_admin_id=actor_admin_id,
                manual_recharge_order_id=order.id,
                manual_recharge_event_id=event.id,
                entry_type="manual_recharge",
                amount_microtickets=order.ticket_amount_microtickets,
                available_delta=order.ticket_amount_microtickets,
                held_delta=0,
                available_after=wallet.available_microtickets,
                held_after=wallet.held_microtickets,
                reason=normalized_reason,
                correlation={
                    "order_number": order.order_number,
                    "event_type": "completed",
                },
            )
            session.add(ledger)
            session.flush()
            event.ledger_entry_id = ledger.id
            self._append_audit(
                session,
                actor_admin_id=actor_admin_id,
                order=order,
                action="manual_recharge_order.complete",
                reason=normalized_reason,
                correlation_id=correlation_id or str(uuid.uuid4()),
                before=before,
                after={**_safe_snapshot(order), "ledger_entry_id": ledger.id},
            )
            session.flush()
            return order

    def cancel(
        self,
        admin: AdminContext,
        order_id: int,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> ManualRechargeOrderRecord:
        actor_admin_id = self._actor_id(admin)
        normalized_reason = self._reason(reason)
        key = self._require_idempotency_key(idempotency_key)
        request_fingerprint = _fingerprint(
            {
                "action": "cancel",
                "order_id": order_id,
                "expected_version": expected_version,
                "reason": normalized_reason,
            }
        )
        with self.database.transaction(admin) as session:
            replay = self._replayed_event(
                session,
                actor_admin_id=actor_admin_id,
                event_type="cancelled",
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                order_id=order_id,
            )
            if replay is not None:
                return replay
            order = self._locked_order(session, order_id)
            if order.version != expected_version:
                raise ManualRechargeConflictError(
                    "订单已被更新，请刷新后重试",
                    code="ORDER_VERSION_CONFLICT",
                    details={"current_version": order.version, "current_status": order.status},
                )
            if order.status != "pending":
                raise ManualRechargeConflictError("只有待确认订单可以取消，已完成订单请走退款")
            before = _safe_snapshot(order)
            order.status = "cancelled"
            order.cancel_reason = normalized_reason
            order.cancelled_by_admin_id = actor_admin_id
            order.cancelled_at = datetime.now(UTC)
            order.version += 1
            session.add(
                ManualRechargeOrderEventRecord(
                    order_id=order.id,
                    user_id=order.user_id,
                    actor_admin_id=actor_admin_id,
                    event_type="cancelled",
                    idempotency_key=key,
                    request_fingerprint=request_fingerprint,
                    version_before=expected_version,
                    version_after=order.version,
                    reason=normalized_reason,
                    snapshot=_safe_snapshot(order),
                )
            )
            self._append_audit(
                session,
                actor_admin_id=actor_admin_id,
                order=order,
                action="manual_recharge_order.cancel",
                reason=normalized_reason,
                correlation_id=correlation_id or str(uuid.uuid4()),
                before=before,
                after=_safe_snapshot(order),
            )
            session.flush()
            return order

    def refund(
        self,
        admin: AdminContext,
        order_id: int,
        *,
        cash_amount_fen: int,
        ticket_amount_microtickets: int,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        correlation_id: str | None = None,
    ) -> ManualRechargeOrderRecord:
        actor_admin_id = self._actor_id(admin)
        cash = self._require_positive(
            cash_amount_fen,
            "退款金额（分）",
            maximum=self.MAX_CASH_AMOUNT_FEN,
        )
        tickets = self._require_positive(
            ticket_amount_microtickets,
            "退回算力券数量",
            maximum=self.MAX_TICKET_AMOUNT_MICROTICKETS,
        )
        normalized_reason = self._reason(reason)
        key = self._require_idempotency_key(idempotency_key)
        request_fingerprint = _fingerprint(
            {
                "action": "refund",
                "order_id": order_id,
                "cash_amount_fen": cash,
                "ticket_amount_microtickets": tickets,
                "expected_version": expected_version,
                "reason": normalized_reason,
            }
        )
        with self.database.transaction(admin) as session:
            replay = self._replayed_event(
                session,
                actor_admin_id=actor_admin_id,
                event_type="refunded",
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                order_id=order_id,
            )
            if replay is not None:
                return replay
            order = self._locked_order(session, order_id)
            if order.version != expected_version:
                raise ManualRechargeConflictError(
                    "订单已被更新，请刷新后重试",
                    code="ORDER_VERSION_CONFLICT",
                    details={"current_version": order.version, "current_status": order.status},
                )
            if order.status not in {"completed", "partially_refunded"}:
                raise ManualRechargeConflictError("只有已完成或部分退款订单可以退款")
            remaining_cash = order.cash_amount_fen - order.refunded_cash_fen
            remaining_tickets = (
                order.ticket_amount_microtickets - order.refunded_microtickets
            )
            if cash > remaining_cash or tickets > remaining_tickets:
                raise ManualRechargeConflictError(
                    "退款金额或算力券超过订单剩余可退额度",
                    code="REFUND_EXCEEDS_ORDER",
                    details={
                        "remaining_cash_fen": remaining_cash,
                        "remaining_microtickets": remaining_tickets,
                    },
                )
            wallet = self._locked_wallet(session, order.user_id)
            if wallet.available_microtickets < tickets:
                shortfall = tickets - wallet.available_microtickets
                raise ManualRechargeConflictError(
                    "用户可用算力券不足，不能完成退款",
                    code="REFUND_BALANCE_SHORTFALL",
                    details={
                        "available_microtickets": wallet.available_microtickets,
                        "held_microtickets": wallet.held_microtickets,
                        "shortfall_microtickets": shortfall,
                    },
                )
            before = _safe_snapshot(order)
            order.refunded_cash_fen += cash
            order.refunded_microtickets += tickets
            order.status = (
                "refunded"
                if order.refunded_cash_fen == order.cash_amount_fen
                and order.refunded_microtickets == order.ticket_amount_microtickets
                else "partially_refunded"
            )
            order.last_refunded_by_admin_id = actor_admin_id
            order.refunded_at = datetime.now(UTC)
            order.version += 1
            wallet.available_microtickets -= tickets
            wallet.lifetime_refunded_microtickets += tickets
            wallet.version += 1
            event = ManualRechargeOrderEventRecord(
                order_id=order.id,
                user_id=order.user_id,
                actor_admin_id=actor_admin_id,
                event_type="refunded",
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                cash_amount_fen=cash,
                ticket_amount_microtickets=tickets,
                version_before=expected_version,
                version_after=order.version,
                reason=normalized_reason,
                snapshot=_safe_snapshot(order),
            )
            session.add(event)
            session.flush()
            ledger = TicketLedgerRecord(
                user_id=order.user_id,
                actor_admin_id=actor_admin_id,
                manual_recharge_order_id=order.id,
                manual_recharge_event_id=event.id,
                entry_type="manual_recharge_refund",
                amount_microtickets=tickets,
                available_delta=-tickets,
                held_delta=0,
                available_after=wallet.available_microtickets,
                held_after=wallet.held_microtickets,
                reason=normalized_reason,
                correlation={
                    "order_number": order.order_number,
                    "event_type": "refunded",
                },
            )
            session.add(ledger)
            session.flush()
            event.ledger_entry_id = ledger.id
            self._append_audit(
                session,
                actor_admin_id=actor_admin_id,
                order=order,
                action="manual_recharge_order.refund",
                reason=normalized_reason,
                correlation_id=correlation_id or str(uuid.uuid4()),
                before=before,
                after={**_safe_snapshot(order), "ledger_entry_id": ledger.id},
            )
            session.flush()
            return order

    def get_detail(
        self,
        admin: AdminContext,
        order_id: int,
    ) -> ManualRechargeOrderDetail:
        self._actor_id(admin)
        with self.database.transaction(admin) as session:
            order = session.get(ManualRechargeOrderRecord, order_id)
            if order is None:
                raise ManualRechargeNotFoundError("人工充值订单不存在")
            events = list(
                session.scalars(
                    select(ManualRechargeOrderEventRecord)
                    .where(ManualRechargeOrderEventRecord.order_id == order.id)
                    .order_by(
                        ManualRechargeOrderEventRecord.created_at.asc(),
                        ManualRechargeOrderEventRecord.id.asc(),
                    )
                )
            )
            ledger = list(
                session.scalars(
                    select(TicketLedgerRecord)
                    .where(TicketLedgerRecord.manual_recharge_order_id == order.id)
                    .order_by(TicketLedgerRecord.created_at.asc(), TicketLedgerRecord.id.asc())
                )
            )
            wallet = session.scalar(
                select(TicketWalletRecord).where(TicketWalletRecord.user_id == order.user_id)
            )
            return ManualRechargeOrderDetail(
                order=order,
                events=events,
                ledger=ledger,
                available_microtickets=wallet.available_microtickets if wallet else 0,
                held_microtickets=wallet.held_microtickets if wallet else 0,
            )

    def list_orders(
        self,
        admin: AdminContext,
        *,
        order_number: str | None = None,
        user_id: int | None = None,
        status: ManualRechargeOrderStatus | None = None,
        actor_admin_id: int | None = None,
        offline_reference: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> ManualRechargeOrderPage:
        self._actor_id(admin)
        filters = []
        if order_number:
            filters.append(ManualRechargeOrderRecord.order_number.ilike(f"%{order_number.strip()}%"))
        if user_id is not None:
            filters.append(ManualRechargeOrderRecord.user_id == user_id)
        if status is not None:
            filters.append(ManualRechargeOrderRecord.status == status)
        if actor_admin_id is not None:
            filters.append(
                or_(
                    ManualRechargeOrderRecord.created_by_admin_id == actor_admin_id,
                    ManualRechargeOrderRecord.completed_by_admin_id == actor_admin_id,
                    ManualRechargeOrderRecord.cancelled_by_admin_id == actor_admin_id,
                    ManualRechargeOrderRecord.last_refunded_by_admin_id == actor_admin_id,
                )
            )
        if offline_reference:
            filters.append(
                ManualRechargeOrderRecord.offline_reference.ilike(
                    f"%{offline_reference.strip()}%"
                )
            )
        if start_at is not None:
            filters.append(ManualRechargeOrderRecord.created_at >= start_at)
        if end_at is not None:
            filters.append(ManualRechargeOrderRecord.created_at < end_at)
        with self.database.transaction(admin) as session:
            totals = session.execute(
                select(
                    func.count(ManualRechargeOrderRecord.id),
                    func.coalesce(func.sum(ManualRechargeOrderRecord.cash_amount_fen), 0),
                    func.coalesce(
                        func.sum(ManualRechargeOrderRecord.ticket_amount_microtickets), 0
                    ),
                ).where(*filters)
            ).one()
            orders = list(
                session.scalars(
                    select(ManualRechargeOrderRecord)
                    .where(*filters)
                    .order_by(
                        ManualRechargeOrderRecord.created_at.desc(),
                        ManualRechargeOrderRecord.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            return ManualRechargeOrderPage(
                items=orders,
                total=int(totals[0]),
                total_cash_fen=int(totals[1]),
                total_ticket_microtickets=int(totals[2]),
                offset=offset,
                limit=limit,
            )

    def reconcile_order(
        self,
        admin: AdminContext,
        order_id: int,
        *,
        reason: str,
        correlation_id: str | None = None,
    ) -> ManualRechargeReconciliationResult:
        actor_admin_id = self._actor_id(admin)
        normalized_reason = self._reason(reason, field_name="对账原因")
        correlation = correlation_id or str(uuid.uuid4())
        with self.database.transaction(admin) as session:
            order = session.get(ManualRechargeOrderRecord, order_id)
            if order is None:
                raise ManualRechargeNotFoundError("人工充值订单不存在")
            events = list(
                session.scalars(
                    select(ManualRechargeOrderEventRecord).where(
                        ManualRechargeOrderEventRecord.order_id == order.id
                    )
                )
            )
            ledger = list(
                session.scalars(
                    select(TicketLedgerRecord).where(
                        TicketLedgerRecord.manual_recharge_order_id == order.id
                    )
                )
            )
            completion_events = [event for event in events if event.event_type == "completed"]
            refund_events = [event for event in events if event.event_type == "refunded"]
            recharge_ledger = [item for item in ledger if item.entry_type == "manual_recharge"]
            refund_ledger = [
                item for item in ledger if item.entry_type == "manual_recharge_refund"
            ]
            expected_completed = order.status in {
                "completed",
                "partially_refunded",
                "refunded",
            }
            mismatches: list[str] = []
            if len([event for event in events if event.event_type == "created"]) != 1:
                mismatches.append("创建事件数量不是 1")
            if expected_completed and len(completion_events) != 1:
                mismatches.append("完成事件数量不是 1")
            if not expected_completed and completion_events:
                mismatches.append("未完成订单存在完成事件")
            if expected_completed and (
                len(recharge_ledger) != 1
                or sum(item.amount_microtickets for item in recharge_ledger)
                != order.ticket_amount_microtickets
            ):
                mismatches.append("充值账本与订单算力券不一致")
            if sum(event.cash_amount_fen for event in refund_events) != order.refunded_cash_fen:
                mismatches.append("退款事件金额与订单累计退款不一致")
            if (
                sum(event.ticket_amount_microtickets for event in refund_events)
                != order.refunded_microtickets
            ):
                mismatches.append("退款事件算力券与订单累计退款不一致")
            if sum(item.amount_microtickets for item in refund_ledger) != order.refunded_microtickets:
                mismatches.append("退款账本与订单累计退款不一致")
            status = "mismatch" if mismatches else "consistent"
            severity = "high" if mismatches else "info"
            details = {
                "order_number": order.order_number,
                "order_status": order.status,
                "event_count": len(events),
                "ledger_count": len(ledger),
                "mismatches": mismatches,
            }
            report = ManualRechargeReconciliationReportRecord(
                order_id=order.id,
                user_id=order.user_id,
                actor_admin_id=actor_admin_id,
                status=status,
                severity=severity,
                details=details,
                correlation_id=correlation,
            )
            session.add(report)
            session.flush()
            self._append_audit(
                session,
                actor_admin_id=actor_admin_id,
                order=order,
                action="manual_recharge_order.reconcile",
                reason=normalized_reason,
                correlation_id=correlation,
                before=None,
                after={"report_id": report.id, "status": status, "severity": severity},
            )
            session.flush()
            return ManualRechargeReconciliationResult(
                report_id=report.id,
                order_id=order.id,
                status=status,
                severity=severity,
                details=details,
            )
