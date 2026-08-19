from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .auth.api import AuthApplication
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .contracts import UserContext
from .ticket_history import (
    PaginatedTicketHistory,
    TicketHistoryConflictError,
    TicketHistoryNotFoundError,
    TicketHistoryService,
    UserLedgerHistoryItem,
    UserUsageHistoryItem,
)
from .ticket_math import MICROTICKETS_PER_TICKET
from .ticket_wallet import TicketWalletSnapshot


def _ticket_text(microtickets: int) -> str:
    whole, fraction = divmod(abs(microtickets), MICROTICKETS_PER_TICKET)
    sign = "-" if microtickets < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _wallet_response(wallet: TicketWalletSnapshot) -> dict[str, str | int]:
    return {
        "available_microtickets": str(wallet.available_microtickets),
        "held_microtickets": str(wallet.held_microtickets),
        "total_microtickets": str(wallet.total_microtickets),
        "available_tickets": _ticket_text(wallet.available_microtickets),
        "held_tickets": _ticket_text(wallet.held_microtickets),
        "total_tickets": _ticket_text(wallet.total_microtickets),
        "version": wallet.version,
    }


def _history_response(history: PaginatedTicketHistory) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in history.items:
        if isinstance(item, UserLedgerHistoryItem):
            items.append(
                {
                    "id": item.id,
                    "entry_type": item.entry_type,
                    "operation_zh": item.operation_zh,
                    "workspace_id": item.workspace_id,
                    "project_id": item.project_id,
                    "task_id": item.task_id,
                    "order_number": item.order_number,
                    "metering_tokens": (
                        str(item.metering_tokens)
                        if item.metering_tokens is not None
                        else None
                    ),
                    "amount_microtickets": str(item.amount_microtickets),
                    "amount_tickets": _ticket_text(item.amount_microtickets),
                    "display_delta_microtickets": str(
                        item.display_delta_microtickets
                    ),
                    "display_delta_tickets": _ticket_text(
                        item.display_delta_microtickets
                    ),
                    "available_after": str(item.available_after),
                    "held_after": str(item.held_after),
                    "available_after_tickets": _ticket_text(item.available_after),
                    "held_after_tickets": _ticket_text(item.held_after),
                    "status": item.status,
                    "status_zh": item.status_zh,
                    "reason": item.reason,
                    "created_at": item.created_at,
                }
            )
        else:
            assert isinstance(item, UserUsageHistoryItem)
            items.append(
                {
                    "id": item.id,
                    "workspace_id": item.workspace_id,
                    "project_id": item.project_id,
                    "task_id": item.task_id,
                    "capability": item.capability,
                    "outcome": item.outcome,
                    "status_zh": item.status_zh,
                    "metering_tokens": str(item.metering_tokens),
                    "charged_microtickets": str(item.charged_microtickets),
                    "charged_tickets": _ticket_text(item.charged_microtickets),
                    "tokens_per_ticket": str(item.tokens_per_ticket),
                    "created_at": item.created_at,
                }
            )
    return {
        "view": history.view,
        "items": items,
        "total": history.total,
        "offset": history.offset,
        "limit": history.limit,
    }


def install_cloud_ticket_history_api(
    app: FastAPI,
    auth: AuthApplication,
) -> TicketHistoryService:
    service = TicketHistoryService(auth.database)
    router = APIRouter(prefix="/wallet", tags=["用户算力券"])

    def require_user(request: Request) -> UserContext:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        principal: SessionPrincipal = auth.sessions.resolve(token)
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
        )

    @router.get("")
    def get_summary(
        identity: UserContext = Depends(require_user),
    ) -> dict[str, str | int]:
        return _wallet_response(service.get_summary(identity))

    @router.get("/history")
    def get_history(
        view: Literal["ledger", "usage"] = Query(default="ledger"),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
        identity: UserContext = Depends(require_user),
    ) -> dict[str, Any]:
        return _history_response(
            service.list_history(
                identity,
                view=view,
                offset=offset,
                limit=limit,
            )
        )

    app.include_router(router)
    app.state.ticket_history_service = service

    @app.exception_handler(TicketHistoryNotFoundError)
    def handle_not_found(_request: Request, exc: TicketHistoryNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "TICKET_WALLET_NOT_FOUND", "message": str(exc)},
        )

    @app.exception_handler(TicketHistoryConflictError)
    def handle_conflict(_request: Request, exc: TicketHistoryConflictError):
        return JSONResponse(
            status_code=422,
            content={"code": "TICKET_HISTORY_INVALID", "message": str(exc)},
        )

    return service
