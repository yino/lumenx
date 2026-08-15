from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .auth.admin import AdminAuthorizationError
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .contracts import UserContext
from .db_models import (
    AITaskRecord,
    AuditEventRecord,
    ImportBatchRecord,
    TicketWalletRecord,
    UsageEventRecord,
    UserRecord,
)
from .ticket_math import MICROTICKETS_PER_TICKET


UserStatus = Literal["active", "suspended"]
TaskStatus = Literal[
    "reserved",
    "queued",
    "running",
    "provider_succeeded",
    "succeeded",
    "failed",
    "cancelled",
    "support_review",
]
UsageOutcome = Literal[
    "succeeded",
    "nonbillable_failure",
    "billable_failure",
    "cancelled",
]
ImportStatus = Literal[
    "pending",
    "dry_run",
    "running",
    "completed",
    "failed",
    "reverted",
]


class AdminCreateUserRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


TASK_STATUS_ZH: dict[str, str] = {
    "reserved": "预扣中",
    "queued": "排队中",
    "running": "执行中",
    "provider_succeeded": "结果处理中",
    "succeeded": "已完成",
    "failed": "已失败",
    "cancelled": "已取消",
    "support_review": "计费待复核",
}

USAGE_OUTCOME_ZH: dict[str, str] = {
    "succeeded": "已结算",
    "nonbillable_failure": "失败未计费",
    "billable_failure": "失败已计费",
    "cancelled": "已取消",
}

IMPORT_STATUS_ZH: dict[str, str] = {
    "pending": "待处理",
    "dry_run": "预检完成",
    "running": "导入中",
    "completed": "已完成",
    "failed": "已失败",
    "reverted": "已回滚",
}


def _ticket_text(microtickets: int) -> str:
    whole, fraction = divmod(abs(microtickets), MICROTICKETS_PER_TICKET)
    sign = "-" if microtickets < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _safe_model_projection(snapshot: dict[str, Any]) -> dict[str, str]:
    return {
        "display_name": str(snapshot.get("display_name") or "平台模型"),
        "model_id": str(snapshot.get("provider_model_id") or ""),
        "provider": str(snapshot.get("provider") or ""),
    }


class PlatformAdministrationQueryService:
    """Read-only platform views that deliberately omit private task content."""

    def __init__(self, database: Any) -> None:
        self.database = database

    @staticmethod
    def _require_admin(identity: UserContext) -> None:
        if not identity.is_platform_admin:
            raise AdminAuthorizationError("仅平台管理员可以查看平台管理数据")

    def list_users(
        self,
        identity: UserContext,
        *,
        status: UserStatus | None,
        query: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if status is not None:
            filters.append(UserRecord.status == status)
        normalized_query = (query or "").strip()
        if normalized_query:
            filters.append(UserRecord.phone_canonical.ilike(f"%{normalized_query}%"))

        with self.database.transaction(identity) as session:
            total = int(
                session.scalar(select(func.count(UserRecord.id)).where(*filters)) or 0
            )
            rows = session.execute(
                select(UserRecord, TicketWalletRecord)
                .outerjoin(TicketWalletRecord, TicketWalletRecord.user_id == UserRecord.id)
                .where(*filters)
                .order_by(UserRecord.created_at.desc(), UserRecord.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()

        return {
            "items": [
                {
                    "id": str(user.id),
                    "phone": user.phone_canonical,
                    "status": user.status,
                    "status_zh": "正常" if user.status == "active" else "已停用",
                    "phone_verified": user.phone_verified_at is not None,
                    "is_platform_admin": user.is_platform_admin,
                    "available_tickets": _ticket_text(wallet.available_microtickets)
                    if wallet is not None
                    else "0",
                    "held_tickets": _ticket_text(wallet.held_microtickets)
                    if wallet is not None
                    else "0",
                    "created_at": _iso(user.created_at),
                    "updated_at": _iso(user.updated_at),
                }
                for user, wallet in rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def list_tasks(
        self,
        identity: UserContext,
        *,
        status: TaskStatus | None,
        user_id: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if status is not None:
            filters.append(AITaskRecord.status == status)
        if user_id is not None:
            filters.append(AITaskRecord.user_id == user_id)

        with self.database.transaction(identity) as session:
            total = int(
                session.scalar(select(func.count(AITaskRecord.id)).where(*filters)) or 0
            )
            rows = session.execute(
                select(AITaskRecord, UserRecord.phone_canonical)
                .join(UserRecord, UserRecord.id == AITaskRecord.user_id)
                .where(*filters)
                .order_by(AITaskRecord.created_at.desc(), AITaskRecord.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()

        return {
            "items": [
                {
                    "id": str(task.id),
                    "user_id": str(task.user_id),
                    "user_phone": phone,
                    "workspace_id": str(task.workspace_id),
                    "project_id": str(task.project_id) if task.project_id else None,
                    "capability": task.capability,
                    "status": task.status,
                    "status_zh": TASK_STATUS_ZH[task.status],
                    "quoted_tickets": _ticket_text(task.quoted_microtickets),
                    "provider_billable": task.provider_billable,
                    "cancellation_requested": task.cancellation_requested_at is not None,
                    "support_review_reason": task.support_review_reason,
                    "safe_error_message": task.safe_error_message,
                    "actual_model": _safe_model_projection(task.config_snapshot),
                    "created_at": _iso(task.created_at),
                    "updated_at": _iso(task.updated_at),
                    "completed_at": _iso(task.completed_at),
                }
                for task, phone in rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def list_usage(
        self,
        identity: UserContext,
        *,
        outcome: UsageOutcome | None,
        user_id: int | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if outcome is not None:
            filters.append(UsageEventRecord.outcome == outcome)
        if user_id is not None:
            filters.append(UsageEventRecord.user_id == user_id)

        with self.database.transaction(identity) as session:
            totals = session.execute(
                select(
                    func.count(UsageEventRecord.id),
                    func.coalesce(func.sum(UsageEventRecord.metering_tokens), 0),
                    func.coalesce(func.sum(UsageEventRecord.charged_microtickets), 0),
                ).where(*filters)
            ).one()
            rows = session.execute(
                select(UsageEventRecord, UserRecord.phone_canonical)
                .join(UserRecord, UserRecord.id == UsageEventRecord.user_id)
                .where(*filters)
                .order_by(UsageEventRecord.created_at.desc(), UsageEventRecord.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()

        return {
            "items": [
                {
                    "id": str(event.id),
                    "user_id": str(event.user_id),
                    "user_phone": phone,
                    "workspace_id": str(event.workspace_id),
                    "project_id": str(event.project_id) if event.project_id else None,
                    "task_id": str(event.task_id),
                    "capability": event.capability,
                    "outcome": event.outcome,
                    "outcome_zh": USAGE_OUTCOME_ZH[event.outcome],
                    "metering_tokens": str(event.metering_tokens),
                    "tokens_per_ticket": str(event.tokens_per_ticket),
                    "charged_tickets": _ticket_text(event.charged_microtickets),
                    "created_at": _iso(event.created_at),
                }
                for event, phone in rows
            ],
            "total": int(totals[0]),
            "total_metering_tokens": str(totals[1]),
            "total_charged_tickets": _ticket_text(int(totals[2])),
            "offset": offset,
            "limit": limit,
        }

    def list_audit_events(
        self,
        identity: UserContext,
        *,
        action: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        normalized_action = (action or "").strip()
        if normalized_action:
            filters.append(AuditEventRecord.action.ilike(f"%{normalized_action}%"))

        with self.database.transaction(identity) as session:
            total = int(
                session.scalar(select(func.count(AuditEventRecord.id)).where(*filters))
                or 0
            )
            events = list(
                session.scalars(
                    select(AuditEventRecord)
                    .where(*filters)
                    .order_by(AuditEventRecord.created_at.desc(), AuditEventRecord.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )

        return {
            "items": [
                {
                    "id": str(event.id),
                    "actor_user_id": str(event.actor_user_id)
                    if event.actor_user_id
                    else None,
                    "target_user_id": str(event.target_user_id)
                    if event.target_user_id
                    else None,
                    "workspace_id": str(event.workspace_id) if event.workspace_id else None,
                    "action": event.action,
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "reason": event.reason,
                    "correlation_id": event.correlation_id,
                    "created_at": _iso(event.created_at),
                }
                for event in events
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def list_import_batches(
        self,
        identity: UserContext,
        *,
        status: ImportStatus | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if status is not None:
            filters.append(ImportBatchRecord.status == status)

        with self.database.transaction(identity) as session:
            total = int(
                session.scalar(select(func.count(ImportBatchRecord.id)).where(*filters))
                or 0
            )
            batches = list(
                session.scalars(
                    select(ImportBatchRecord)
                    .where(*filters)
                    .order_by(ImportBatchRecord.created_at.desc(), ImportBatchRecord.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )

        return {
            "items": [
                {
                    "id": str(batch.id),
                    "actor_admin_user_id": str(batch.actor_admin_user_id),
                    "target_user_id": str(batch.target_user_id),
                    "target_workspace_id": str(batch.target_workspace_id),
                    "source_fingerprint": batch.source_fingerprint,
                    "status": batch.status,
                    "status_zh": IMPORT_STATUS_ZH[batch.status],
                    "has_dry_run_report": batch.dry_run_report is not None,
                    "has_result_report": batch.result_report is not None,
                    "has_error_report": batch.error_report is not None,
                    "created_at": _iso(batch.created_at),
                    "started_at": _iso(batch.started_at),
                    "completed_at": _iso(batch.completed_at),
                    "reverted_at": _iso(batch.reverted_at),
                    "rollback_reason": batch.rollback_reason,
                }
                for batch in batches
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }


def install_cloud_platform_administration_api(
    app: FastAPI,
    auth: AuthApplication,
) -> PlatformAdministrationQueryService:
    service = PlatformAdministrationQueryService(auth.database)
    router = APIRouter(prefix="/admin", tags=["平台管理"])

    def require_admin(request: Request) -> UserContext:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf_token = (
            request.headers.get("x-csrf-token")
            if request.method in UNSAFE_METHODS
            else None
        )
        principal: SessionPrincipal = auth.sessions.resolve(token, csrf_token=csrf_token)
        if not principal.is_platform_admin:
            raise AdminAuthorizationError("仅平台管理员可以查看平台管理数据")
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=True,
        )

    @router.get("/users")
    def list_users(
        status: UserStatus | None = Query(default=None),
        query: str | None = Query(default=None, max_length=32),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_users(
            identity, status=status, query=query, offset=offset, limit=limit
        )

    @router.post("/users", status_code=201)
    def create_user(
        payload: AdminCreateUserRequest,
        request: Request,
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, str]:
        if auth.runtime_policy is None:
            raise RuntimeError("平台运行策略服务尚未就绪")
        policy = auth.runtime_policy.resolve()
        created = auth.user_administration.create_user(
            identity,
            payload.phone,
            payload.password,
            payload.reason,
            initial_grant_microtickets=(
                policy.registration_initial_grant_microtickets
            ),
            config_version_id=policy.config_version_id,
            correlation_id=getattr(request.state, "correlation_id", None),
        )
        return {
            "id": str(created.user_id),
            "workspace_id": str(created.workspace_id),
            "phone": created.phone_canonical,
            "status": "active",
        }

    @router.get("/tasks")
    def list_tasks(
        status: TaskStatus | None = Query(default=None),
        user_id: int | None = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_tasks(
            identity, status=status, user_id=user_id, offset=offset, limit=limit
        )

    @router.get("/usage")
    def list_usage(
        outcome: UsageOutcome | None = Query(default=None),
        user_id: int | None = Query(default=None, ge=1),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_usage(
            identity, outcome=outcome, user_id=user_id, offset=offset, limit=limit
        )

    @router.get("/audit-events")
    def list_audit_events(
        action: str | None = Query(default=None, max_length=120),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_audit_events(
            identity, action=action, offset=offset, limit=limit
        )

    @router.get("/import-batches")
    def list_import_batches(
        status: ImportStatus | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_import_batches(
            identity, status=status, offset=offset, limit=limit
        )

    app.include_router(router)
    app.state.platform_administration_query_service = service
    return service
