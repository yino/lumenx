from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select

from .auth.api import AuthApplication
from .admin_access import create_require_platform_admin, require_platform_admin_context
from .admin_contracts import validate_bounded_window
from .admin_inspection import mask_phone
from .contracts import AdminContext
from .db_models import (
    AITaskRecord,
    AuditEventRecord,
    ImportBatchRecord,
    TicketWalletRecord,
    UsageEventRecord,
    UserRecord,
    WorkspaceRecord,
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
    def _require_admin(identity: AdminContext) -> None:
        require_platform_admin_context(identity)

    def list_users(
        self,
        identity: AdminContext,
        *,
        status: UserStatus | None,
        query: str | None,
        user_id: int | None = None,
        wallet_exception: bool | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if status is not None:
            filters.append(UserRecord.status == status)
        if user_id is not None:
            filters.append(UserRecord.id == user_id)
        if start_at is not None and end_at is not None:
            start_at, end_at = validate_bounded_window(start_at, end_at)
        if start_at is not None:
            filters.append(UserRecord.created_at >= start_at)
        if end_at is not None:
            filters.append(UserRecord.created_at < end_at)
        normalized_query = (query or "").strip()
        if normalized_query:
            filters.append(
                or_(
                    UserRecord.phone_canonical.ilike(f"%{normalized_query}%"),
                    UserRecord.username.ilike(f"%{normalized_query.lower()}%"),
                )
            )
        if wallet_exception is True:
            filters.append(TicketWalletRecord.id.is_(None))
        elif wallet_exception is False:
            filters.append(TicketWalletRecord.id.is_not(None))

        with self.database.transaction(identity) as session:
            total = int(
                session.scalar(
                    select(func.count(UserRecord.id))
                    .outerjoin(TicketWalletRecord, TicketWalletRecord.user_id == UserRecord.id)
                    .where(*filters)
                )
                or 0
            )
            rows = session.execute(
                select(UserRecord, TicketWalletRecord)
                .outerjoin(TicketWalletRecord, TicketWalletRecord.user_id == UserRecord.id)
                .where(*filters)
                .order_by(UserRecord.created_at.desc(), UserRecord.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            user_ids = [user.id for user, _wallet in rows]
            workspace_counts = {
                row.user_id: int(row.count)
                for row in session.execute(
                    select(
                        WorkspaceRecord.user_id,
                        func.count(WorkspaceRecord.id).label("count"),
                    )
                    .where(
                        WorkspaceRecord.user_id.in_(user_ids or [-1]),
                        WorkspaceRecord.deleted_at.is_(None),
                    )
                    .group_by(WorkspaceRecord.user_id)
                )
            }
            task_summaries = {
                row.user_id: {
                    "total": int(row.total),
                    "exceptions": int(row.exceptions or 0),
                }
                for row in session.execute(
                    select(
                        AITaskRecord.user_id,
                        func.count(AITaskRecord.id).label("total"),
                        func.sum(
                            case(
                                (AITaskRecord.status.in_(("failed", "support_review")), 1),
                                else_=0,
                            )
                        ).label("exceptions"),
                    )
                    .where(AITaskRecord.user_id.in_(user_ids or [-1]))
                    .group_by(AITaskRecord.user_id)
                )
            }

        return {
            "items": [
                {
                    "id": str(user.id),
                    "username": user.username,
                    "account_label": user.username or mask_phone(user.phone_canonical) or f"用户 {user.id}",
                    "phone": mask_phone(user.phone_canonical),
                    "status": user.status,
                    "status_zh": "正常" if user.status == "active" else "已停用",
                    "phone_verified": user.phone_verified_at is not None,
                    "available_tickets": _ticket_text(wallet.available_microtickets)
                    if wallet is not None
                    else "0",
                    "held_tickets": _ticket_text(wallet.held_microtickets)
                    if wallet is not None
                    else "0",
                    "wallet_exception": wallet is None,
                    "workspace_count": workspace_counts.get(user.id, 0),
                    "task_summary": task_summaries.get(
                        user.id,
                        {"total": 0, "exceptions": 0},
                    ),
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
        identity: AdminContext,
        *,
        status: TaskStatus | None,
        user_id: int | None,
        workspace_id: int | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if status is not None:
            filters.append(AITaskRecord.status == status)
        if user_id is not None:
            filters.append(AITaskRecord.user_id == user_id)
        if workspace_id is not None:
            filters.append(AITaskRecord.workspace_id == workspace_id)
        if start_at is not None and end_at is not None:
            start_at, end_at = validate_bounded_window(start_at, end_at)
        if start_at is not None:
            filters.append(AITaskRecord.created_at >= start_at)
        if end_at is not None:
            filters.append(AITaskRecord.created_at < end_at)

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
                    "user_phone": mask_phone(phone),
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
        identity: AdminContext,
        *,
        outcome: UsageOutcome | None,
        user_id: int | None,
        workspace_id: int | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if outcome is not None:
            filters.append(UsageEventRecord.outcome == outcome)
        if user_id is not None:
            filters.append(UsageEventRecord.user_id == user_id)
        if workspace_id is not None:
            filters.append(UsageEventRecord.workspace_id == workspace_id)
        if start_at is not None and end_at is not None:
            start_at, end_at = validate_bounded_window(start_at, end_at)
        if start_at is not None:
            filters.append(UsageEventRecord.created_at >= start_at)
        if end_at is not None:
            filters.append(UsageEventRecord.created_at < end_at)

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
                    "user_phone": mask_phone(phone),
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
        identity: AdminContext,
        *,
        action: str | None,
        actor_user_id: int | None = None,
        actor_admin_id: int | None = None,
        target_user_id: int | None = None,
        workspace_id: int | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        purpose: str | None = None,
        correlation_id: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        normalized_action = (action or "").strip()
        if normalized_action:
            filters.append(AuditEventRecord.action.ilike(f"%{normalized_action}%"))
        if actor_user_id is not None:
            filters.append(AuditEventRecord.actor_user_id == actor_user_id)
        if actor_admin_id is not None:
            filters.append(AuditEventRecord.actor_admin_id == actor_admin_id)
        if target_user_id is not None:
            filters.append(AuditEventRecord.target_user_id == target_user_id)
        if workspace_id is not None:
            filters.append(AuditEventRecord.workspace_id == workspace_id)
        if target_type:
            filters.append(AuditEventRecord.target_type == target_type.strip())
        if target_id:
            filters.append(AuditEventRecord.target_id == target_id.strip())
        if purpose:
            filters.append(AuditEventRecord.reason.ilike(f"%{purpose.strip()}%"))
        if correlation_id:
            filters.append(AuditEventRecord.correlation_id == correlation_id.strip())
        if start_at is not None and end_at is not None:
            start_at, end_at = validate_bounded_window(start_at, end_at)
        if start_at is not None:
            filters.append(AuditEventRecord.created_at >= start_at)
        if end_at is not None:
            filters.append(AuditEventRecord.created_at < end_at)

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
                    "actor_admin_id": str(event.actor_admin_id)
                    if event.actor_admin_id
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
        identity: AdminContext,
        *,
        status: ImportStatus | None,
        user_id: int | None = None,
        workspace_id: int | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._require_admin(identity)
        filters = []
        if status is not None:
            filters.append(ImportBatchRecord.status == status)
        if user_id is not None:
            filters.append(ImportBatchRecord.target_user_id == user_id)
        if workspace_id is not None:
            filters.append(ImportBatchRecord.target_workspace_id == workspace_id)
        if start_at is not None and end_at is not None:
            start_at, end_at = validate_bounded_window(start_at, end_at)
        if start_at is not None:
            filters.append(ImportBatchRecord.created_at >= start_at)
        if end_at is not None:
            filters.append(ImportBatchRecord.created_at < end_at)

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
                    "actor_admin_id": str(batch.actor_admin_id)
                    if batch.actor_admin_id is not None
                    else None,
                    "legacy_actor_user_id": str(batch.actor_admin_user_id)
                    if batch.actor_admin_user_id is not None
                    else None,
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

    require_admin = create_require_platform_admin(
        auth.admin_sessions,
        denied_message="仅平台管理员可以查看平台管理数据",
    )

    @router.get("/users")
    def list_users(
        status: UserStatus | None = Query(default=None),
        query: str | None = Query(default=None, max_length=32),
        user_id: int | None = Query(default=None, ge=1),
        wallet_exception: bool | None = Query(default=None),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_users(
            identity,
            status=status,
            query=query,
            user_id=user_id,
            wallet_exception=wallet_exception,
            start_at=start_at,
            end_at=end_at,
            offset=offset,
            limit=limit,
        )

    @router.post("/users", status_code=201)
    def create_user(
        payload: AdminCreateUserRequest,
        request: Request,
        identity: AdminContext = Depends(require_admin),
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
        workspace_id: int | None = Query(default=None, ge=1),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_tasks(
            identity,
            status=status,
            user_id=user_id,
            workspace_id=workspace_id,
            start_at=start_at,
            end_at=end_at,
            offset=offset,
            limit=limit,
        )

    @router.get("/usage")
    def list_usage(
        outcome: UsageOutcome | None = Query(default=None),
        user_id: int | None = Query(default=None, ge=1),
        workspace_id: int | None = Query(default=None, ge=1),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_usage(
            identity,
            outcome=outcome,
            user_id=user_id,
            workspace_id=workspace_id,
            start_at=start_at,
            end_at=end_at,
            offset=offset,
            limit=limit,
        )

    @router.get("/audit-events")
    def list_audit_events(
        action: str | None = Query(default=None, max_length=120),
        actor_user_id: int | None = Query(default=None, ge=1),
        actor_admin_id: int | None = Query(default=None, ge=1),
        target_user_id: int | None = Query(default=None, ge=1),
        workspace_id: int | None = Query(default=None, ge=1),
        target_type: str | None = Query(default=None, max_length=80),
        target_id: str | None = Query(default=None, max_length=160),
        purpose: str | None = Query(default=None, max_length=2000),
        correlation_id: str | None = Query(default=None, max_length=80),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_audit_events(
            identity,
            action=action,
            actor_user_id=actor_user_id,
            actor_admin_id=actor_admin_id,
            target_user_id=target_user_id,
            workspace_id=workspace_id,
            target_type=target_type,
            target_id=target_id,
            purpose=purpose,
            correlation_id=correlation_id,
            start_at=start_at,
            end_at=end_at,
            offset=offset,
            limit=limit,
        )

    @router.get("/import-batches")
    def list_import_batches(
        status: ImportStatus | None = Query(default=None),
        user_id: int | None = Query(default=None, ge=1),
        workspace_id: int | None = Query(default=None, ge=1),
        start_at: datetime | None = Query(default=None),
        end_at: datetime | None = Query(default=None),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=30, ge=1, le=100),
        identity: AdminContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return service.list_import_batches(
            identity,
            status=status,
            user_id=user_id,
            workspace_id=workspace_id,
            start_at=start_at,
            end_at=end_at,
            offset=offset,
            limit=limit,
        )

    app.include_router(router)
    app.state.platform_administration_query_service = service
    return service
