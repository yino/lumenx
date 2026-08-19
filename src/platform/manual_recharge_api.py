from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, PositiveInt

from .admin_access import (
    AdminConsoleUnavailableError,
    create_require_platform_admin,
    enforce_admin_rate_limit,
    require_admin_console_enabled,
)
from .admin_contracts import (
    ADMIN_DEFAULT_PAGE_SIZE,
    ADMIN_MAX_PAGE_SIZE,
    ManualRechargeOrderStatus,
    ORDER_EVENT_ZH,
    ORDER_STATUS_ZH,
)
from .auth.api import AuthApplication
from .contracts import AdminContext
from .error_protocol import request_correlation_id
from .manual_recharge import (
    ManualRechargeConflictError,
    ManualRechargeNotFoundError,
    ManualRechargeOrderDetail,
    ManualRechargeOrderPage,
    ManualRechargeOrderService,
)
from .observability import metrics
from .settings import DeploymentSettings
from .ticket_math import MICROTICKETS_PER_TICKET


class ManualRechargeCreateRequest(BaseModel):
    user_id: PositiveInt
    cash_amount_fen: int = Field(gt=0, le=9_999_999_999_999)
    ticket_amount_microtickets: int = Field(gt=0, le=9_999_999_999_999_999)
    offline_reference: str | None = Field(default=None, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


class ManualRechargeTransitionRequest(BaseModel):
    expected_version: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)


class ManualRechargeRefundRequest(ManualRechargeTransitionRequest):
    cash_amount_fen: int = Field(gt=0, le=9_999_999_999_999)
    ticket_amount_microtickets: int = Field(gt=0, le=9_999_999_999_999_999)


class ManualRechargeReconcileRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _tickets(value: int) -> str:
    whole, fraction = divmod(abs(value), MICROTICKETS_PER_TICKET)
    sign = "-" if value < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _order_response(order: Any) -> dict[str, Any]:
    return {
        "id": str(order.id),
        "order_number": order.order_number,
        "user_id": str(order.user_id),
        "cash_amount_fen": str(order.cash_amount_fen),
        "cash_amount_yuan": f"{order.cash_amount_fen / 100:.2f}",
        "ticket_amount_microtickets": str(order.ticket_amount_microtickets),
        "ticket_amount": _tickets(order.ticket_amount_microtickets),
        "currency": order.currency,
        "status": order.status,
        "status_zh": ORDER_STATUS_ZH[order.status],
        "exchange_snapshot": order.exchange_snapshot,
        "offline_reference": order.offline_reference,
        "create_reason": order.create_reason,
        "cancel_reason": order.cancel_reason,
        "refunded_cash_fen": str(order.refunded_cash_fen),
        "refunded_microtickets": str(order.refunded_microtickets),
        "remaining_refundable_cash_fen": str(
            order.cash_amount_fen - order.refunded_cash_fen
        ),
        "remaining_refundable_microtickets": str(
            order.ticket_amount_microtickets - order.refunded_microtickets
        ),
        "version": order.version,
        "created_by_admin_id": str(order.created_by_admin_id)
        if order.created_by_admin_id
        else None,
        "completed_by_admin_id": str(order.completed_by_admin_id)
        if order.completed_by_admin_id
        else None,
        "cancelled_by_admin_id": str(order.cancelled_by_admin_id)
        if order.cancelled_by_admin_id
        else None,
        "last_refunded_by_admin_id": str(order.last_refunded_by_admin_id)
        if order.last_refunded_by_admin_id
        else None,
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
        "completed_at": _iso(order.completed_at),
        "cancelled_at": _iso(order.cancelled_at),
        "refunded_at": _iso(order.refunded_at),
        "manual_confirmation": True,
    }


def _page_response(page: ManualRechargeOrderPage) -> dict[str, Any]:
    return {
        "items": [_order_response(order) for order in page.items],
        "total": page.total,
        "total_cash_fen": str(page.total_cash_fen),
        "total_ticket_microtickets": str(page.total_ticket_microtickets),
        "offset": page.offset,
        "limit": page.limit,
    }


def _detail_response(detail: ManualRechargeOrderDetail) -> dict[str, Any]:
    return {
        "order": _order_response(detail.order),
        "wallet": {
            "available_microtickets": str(detail.available_microtickets),
            "available_tickets": _tickets(detail.available_microtickets),
            "held_microtickets": str(detail.held_microtickets),
            "held_tickets": _tickets(detail.held_microtickets),
        },
        "events": [
            {
                "id": str(event.id),
                "event_type": event.event_type,
                "event_type_zh": ORDER_EVENT_ZH[event.event_type],
                "actor_admin_id": str(event.actor_admin_id)
                if event.actor_admin_id
                else None,
                "cash_amount_fen": str(event.cash_amount_fen),
                "ticket_amount_microtickets": str(event.ticket_amount_microtickets),
                "ledger_entry_id": str(event.ledger_entry_id)
                if event.ledger_entry_id
                else None,
                "version_before": event.version_before,
                "version_after": event.version_after,
                "reason": event.reason,
                "snapshot": event.snapshot,
                "created_at": _iso(event.created_at),
            }
            for event in detail.events
        ],
        "ledger": [
            {
                "id": str(item.id),
                "entry_type": item.entry_type,
                "amount_microtickets": str(item.amount_microtickets),
                "available_delta": str(item.available_delta),
                "available_after": str(item.available_after),
                "held_after": str(item.held_after),
                "actor_admin_id": str(item.actor_admin_id)
                if item.actor_admin_id
                else None,
                "reason": item.reason,
                "created_at": _iso(item.created_at),
            }
            for item in detail.ledger
        ],
    }


def install_cloud_manual_recharge_api(
    app: FastAPI,
    auth: AuthApplication,
    settings: DeploymentSettings,
) -> ManualRechargeOrderService:
    service = ManualRechargeOrderService(auth.database)
    router = APIRouter(prefix="/admin/recharge-orders", tags=["人工充值订单"])
    require_admin = create_require_platform_admin(auth.admin_sessions)

    def require_console(
        identity: AdminContext = Depends(require_admin),
    ) -> AdminContext:
        require_admin_console_enabled(settings)
        return identity

    @router.get("")
    def list_orders(
        order_number: str | None = Query(default=None, max_length=40),
        user_id: int | None = Query(default=None, ge=1),
        status: ManualRechargeOrderStatus | None = Query(default=None),
        actor_admin_id: int | None = Query(default=None, ge=1),
        offline_reference: str | None = Query(default=None, max_length=160),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=ADMIN_DEFAULT_PAGE_SIZE, ge=1, le=ADMIN_MAX_PAGE_SIZE),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return _page_response(
            service.list_orders(
                identity,
                order_number=order_number,
                user_id=user_id,
                status=status,
                actor_admin_id=actor_admin_id,
                offline_reference=offline_reference,
                start_at=start_at,
                end_at=end_at,
                offset=offset,
                limit=limit,
            )
        )

    @router.post("", status_code=201)
    def create_order(
        payload: ManualRechargeCreateRequest,
        request: Request,
        idempotency_key: str = Header(min_length=1, max_length=160, alias="Idempotency-Key"),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="recharge_order_create")
        if auth.runtime_policy is None:
            raise RuntimeError("平台运行策略服务尚未就绪")
        policy = auth.runtime_policy.resolve()
        order = service.create_pending(
            identity,
            user_id=payload.user_id,
            cash_amount_fen=payload.cash_amount_fen,
            ticket_amount_microtickets=payload.ticket_amount_microtickets,
            offline_reference=payload.offline_reference,
            reason=payload.reason,
            idempotency_key=idempotency_key,
            exchange_snapshot={
                "config_version_id": policy.config_version_id,
                "tokens_per_ticket": policy.tokens_per_ticket,
                "microtickets_per_ticket": MICROTICKETS_PER_TICKET,
                "cash_currency": "CNY",
            },
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_recharge_order_transitions_total", labels={"action": "create", "outcome": "succeeded"})
        return _order_response(order)

    @router.get("/{order_id}")
    def get_order(
        order_id: int,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        return _detail_response(service.get_detail(identity, order_id))

    @router.post("/{order_id}/complete")
    def complete_order(
        order_id: int,
        payload: ManualRechargeTransitionRequest,
        request: Request,
        idempotency_key: str = Header(min_length=1, max_length=160, alias="Idempotency-Key"),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="recharge_order_complete")
        order = service.complete(
            identity,
            order_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            idempotency_key=idempotency_key,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_recharge_order_transitions_total", labels={"action": "complete", "outcome": "succeeded"})
        return _order_response(order)

    @router.post("/{order_id}/cancel")
    def cancel_order(
        order_id: int,
        payload: ManualRechargeTransitionRequest,
        request: Request,
        idempotency_key: str = Header(min_length=1, max_length=160, alias="Idempotency-Key"),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="recharge_order_cancel")
        order = service.cancel(
            identity,
            order_id,
            expected_version=payload.expected_version,
            reason=payload.reason,
            idempotency_key=idempotency_key,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment(
            "admin_recharge_order_transitions_total",
            labels={"action": "cancel", "outcome": "succeeded"},
        )
        return _order_response(order)

    @router.post("/{order_id}/refund")
    def refund_order(
        order_id: int,
        payload: ManualRechargeRefundRequest,
        request: Request,
        idempotency_key: str = Header(min_length=1, max_length=160, alias="Idempotency-Key"),
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="recharge_order_refund")
        order = service.refund(
            identity,
            order_id,
            cash_amount_fen=payload.cash_amount_fen,
            ticket_amount_microtickets=payload.ticket_amount_microtickets,
            expected_version=payload.expected_version,
            reason=payload.reason,
            idempotency_key=idempotency_key,
            correlation_id=request_correlation_id(request),
        )
        metrics.increment("admin_recharge_order_transitions_total", labels={"action": "refund", "outcome": "succeeded"})
        return _order_response(order)

    @router.post("/{order_id}/reconcile")
    def reconcile_order(
        order_id: int,
        payload: ManualRechargeReconcileRequest,
        request: Request,
        identity: AdminContext = Depends(require_console),
    ) -> dict[str, Any]:
        require_admin_console_enabled(settings, mutation=True)
        enforce_admin_rate_limit(auth, settings, request, identity, action="recharge_order_reconcile")
        result = service.reconcile_order(
            identity,
            order_id,
            reason=payload.reason,
            correlation_id=request_correlation_id(request),
        )
        if result.status == "mismatch":
            metrics.increment("admin_recharge_reconciliation_mismatches_total")
        metrics.increment(
            "admin_recharge_reconciliations_total",
            labels={"outcome": result.status},
        )
        return {
            "report_id": str(result.report_id),
            "order_id": str(result.order_id),
            "status": result.status,
            "status_zh": "存在差异" if result.status == "mismatch" else "对账一致",
            "severity": result.severity,
            "details": result.details,
        }

    app.include_router(router)
    app.state.manual_recharge_order_service = service

    @app.exception_handler(ManualRechargeNotFoundError)
    def handle_not_found(request: Request, exc: ManualRechargeNotFoundError):
        return JSONResponse(
            status_code=404,
            content={
                "code": "MANUAL_RECHARGE_NOT_FOUND",
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    @app.exception_handler(ManualRechargeConflictError)
    def handle_conflict(request: Request, exc: ManualRechargeConflictError):
        metrics.increment(
            "admin_recharge_order_conflicts_total",
            labels={"error_code": exc.code},
        )
        return JSONResponse(
            status_code=409,
            content={
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
                "correlation_id": request_correlation_id(request),
            },
        )

    @app.exception_handler(AdminConsoleUnavailableError)
    def handle_console_unavailable(request: Request, exc: AdminConsoleUnavailableError):
        return JSONResponse(
            status_code=503,
            content={
                "code": exc.code,
                "message": str(exc),
                "correlation_id": request_correlation_id(request),
            },
        )

    return service
