from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .admin_access import create_require_platform_admin
from .auth.api import AuthApplication
from .contracts import AdminContext
from .ticket_administration import (
    AdminTicketWalletView,
    TicketAdministrationConflictError,
    TicketAdministrationNotFoundError,
    TicketAdministrationService,
    TicketAdjustmentOperation,
)
from .ticket_math import MICROTICKETS_PER_TICKET
from .ticket_wallet import TicketWalletSnapshot


class AdminTicketAdjustmentRequest(BaseModel):
    amount_tickets: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    reason: str = Field(min_length=1, max_length=2000)


def _microtickets(value: Decimal) -> int:
    scaled = value * MICROTICKETS_PER_TICKET
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise TicketAdministrationConflictError("算力券数量最多保留 6 位小数")
    return int(integral)


def _ticket_text(microtickets: int) -> str:
    whole, fraction = divmod(abs(microtickets), MICROTICKETS_PER_TICKET)
    sign = "-" if microtickets < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _wallet_response(wallet: TicketWalletSnapshot) -> dict[str, str | int]:
    return {
        "user_id": wallet.user_id,
        "available_microtickets": str(wallet.available_microtickets),
        "held_microtickets": str(wallet.held_microtickets),
        "total_microtickets": str(wallet.total_microtickets),
        "available_tickets": _ticket_text(wallet.available_microtickets),
        "held_tickets": _ticket_text(wallet.held_microtickets),
        "total_tickets": _ticket_text(wallet.total_microtickets),
        "version": wallet.version,
    }


def _view_response(view: AdminTicketWalletView) -> dict[str, Any]:
    return {
        "wallet": _wallet_response(view.wallet),
        "ledger": [
            {
                "id": item.id,
                "entry_type": item.entry_type,
                "amount_microtickets": str(item.amount_microtickets),
                "amount_tickets": _ticket_text(item.amount_microtickets),
                "available_delta": str(item.available_delta),
                "held_delta": str(item.held_delta),
                "available_after": str(item.available_after),
                "held_after": str(item.held_after),
                "reason": item.reason,
                "actor_admin_id": item.actor_admin_id,
                "legacy_actor_user_id": item.legacy_actor_user_id,
                "correlation": item.correlation,
                "created_at": item.created_at,
            }
            for item in view.ledger
        ],
        "total": view.total_ledger_entries,
    }


def install_cloud_ticket_administration_api(
    app: FastAPI,
    auth: AuthApplication,
) -> TicketAdministrationService:
    service = TicketAdministrationService(auth.database)
    router = APIRouter(prefix="/admin/tickets", tags=["算力券管理"])

    require_admin = create_require_platform_admin(
        auth.admin_sessions,
        denied_message="仅平台管理员可以管理用户算力券",
    )

    @router.get("/users/{user_id}")
    def get_wallet(
        user_id: int,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return _view_response(
            service.get_wallet(identity, user_id, offset=offset, limit=limit)
        )

    def adjust(
        operation: TicketAdjustmentOperation,
        user_id: int,
        payload: AdminTicketAdjustmentRequest,
        request: Request,
        identity: AdminContext,
    ) -> dict[str, Any]:
        wallet = service.adjust(
            identity,
            user_id,
            operation=operation,
            amount_microtickets=_microtickets(payload.amount_tickets),
            reason=payload.reason,
            correlation_id=request.headers.get("x-correlation-id"),
        )
        return {
            "message": {
                "grant": "算力券已赠送",
                "debit": "算力券已扣减",
                "compensation": "算力券补偿已入账",
            }[operation],
            "wallet": _wallet_response(wallet),
        }

    @router.post("/users/{user_id}/grant")
    def grant(
        user_id: int,
        payload: AdminTicketAdjustmentRequest,
        request: Request,
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return adjust("grant", user_id, payload, request, identity)

    @router.post("/users/{user_id}/debit")
    def debit(
        user_id: int,
        payload: AdminTicketAdjustmentRequest,
        request: Request,
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return adjust("debit", user_id, payload, request, identity)

    @router.post("/users/{user_id}/compensate")
    def compensate(
        user_id: int,
        payload: AdminTicketAdjustmentRequest,
        request: Request,
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return adjust("compensation", user_id, payload, request, identity)

    app.include_router(router)
    app.state.ticket_administration_service = service

    @app.exception_handler(TicketAdministrationNotFoundError)
    def handle_not_found(_request: Request, exc: TicketAdministrationNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "TICKET_WALLET_NOT_FOUND", "message": str(exc)},
        )

    @app.exception_handler(TicketAdministrationConflictError)
    def handle_conflict(_request: Request, exc: TicketAdministrationConflictError):
        return JSONResponse(
            status_code=409,
            content={"code": "TICKET_ADJUSTMENT_CONFLICT", "message": str(exc)},
        )

    return service
