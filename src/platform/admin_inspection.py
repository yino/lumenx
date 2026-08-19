from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select

from .admin_access import require_platform_admin_context
from .admin_contracts import (
    ADMIN_EXPORT_ROW_CAP,
    ORDER_STATUS_ZH,
    normalize_chinese_reason,
    validate_bounded_window,
)
from .contracts import AdminContext
from .db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    AssetRecord,
    AuditEventRecord,
    AuthSessionRecord,
    ManualRechargeOrderRecord,
    ManualRechargeReconciliationReportRecord,
    MediaObjectRecord,
    ProjectRecord,
    SeriesRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
    UserRecord,
    WorkspaceRecord,
)
from .identifiers import parse_database_id
from .media_storage import CloudMediaStorage, MediaStorageError
from .ticket_math import MICROTICKETS_PER_TICKET


InspectionResource = Literal[
    "workspaces",
    "projects",
    "series",
    "assets",
    "media",
    "tasks",
    "attempts",
    "usage",
    "ledger",
    "orders",
    "sessions",
    "audit",
]
ExportResource = Literal["users", "orders", "usage", "tasks"]


class AdminInspectionNotFoundError(LookupError):
    pass


class AdminInspectionValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "ADMIN_INSPECTION_INVALID") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TargetScope:
    user: UserRecord
    workspace: WorkspaceRecord | None


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    if len(phone) <= 7:
        return phone[:2] + "***" + phone[-2:]
    return phone[:4] + "****" + phone[-4:]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _tickets(value: int) -> str:
    whole, fraction = divmod(abs(value), MICROTICKETS_PER_TICKET)
    sign = "-" if value < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _safe_model(snapshot: dict[str, Any] | None) -> dict[str, str]:
    value = snapshot or {}
    return {
        "display_name": str(value.get("display_name") or "平台模型"),
        "provider": str(value.get("provider") or ""),
        "model_id": str(value.get("provider_model_id") or ""),
    }


def _extract_media_ids(value: Any, *, limit: int = 50) -> list[str]:
    found: list[str] = []

    def visit(current: Any, key: str = "") -> None:
        if len(found) >= limit:
            return
        if isinstance(current, dict):
            for child_key, child in current.items():
                visit(child, str(child_key))
        elif isinstance(current, list):
            for child in current:
                visit(child, key)
        elif key.endswith("media_id") and str(current).isdigit():
            canonical = str(current)
            if canonical not in found:
                found.append(canonical)
        elif key.endswith("media_ids") and str(current).isdigit():
            canonical = str(current)
            if canonical not in found:
                found.append(canonical)

    visit(value)
    return found


class AdminInspectionService:
    STALLED_AFTER = timedelta(minutes=30)
    SENSITIVE_TEXT_LIMIT = 100_000

    def __init__(
        self,
        database: Any,
        media_storage: CloudMediaStorage | None = None,
    ) -> None:
        self.database = database
        self.media_storage = media_storage

    @staticmethod
    def _admin_id(identity: AdminContext) -> int:
        require_platform_admin_context(identity)
        return parse_database_id(identity.admin_id, field="管理员 ID")

    @staticmethod
    def _target_scope(
        session: Any,
        user_id: int,
        workspace_id: int | None,
    ) -> TargetScope:
        user = session.get(UserRecord, user_id)
        if user is None:
            raise AdminInspectionNotFoundError("目标用户不存在")
        workspace = None
        if workspace_id is not None:
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.user_id == user_id,
                    WorkspaceRecord.deleted_at.is_(None),
                )
            )
            if workspace is None:
                raise AdminInspectionNotFoundError("目标工作区不属于该用户")
        return TargetScope(user=user, workspace=workspace)

    @staticmethod
    def _workspace_filter(model: Any, workspace_id: int | None) -> list[Any]:
        return [model.workspace_id == workspace_id] if workspace_id is not None else []

    @staticmethod
    def _audit_sensitive(
        session: Any,
        *,
        actor_id: int,
        user_id: int,
        workspace_id: int | None,
        action: str,
        target_type: str,
        target_id: int,
        purpose: str,
        correlation_id: str | None,
    ) -> None:
        session.add(
            AuditEventRecord(
                actor_admin_id=actor_id,
                target_user_id=user_id,
                workspace_id=workspace_id,
                action=action,
                target_type=target_type,
                target_id=str(target_id),
                reason=purpose,
                after_summary={"sensitive_access": True},
                correlation_id=correlation_id or str(uuid.uuid4()),
            )
        )
        session.flush()

    def user_overview(self, admin: AdminContext, user_id: int) -> dict[str, Any]:
        self._admin_id(admin)
        with self.database.transaction(admin) as session:
            scope = self._target_scope(session, user_id, None)
            wallet = session.scalar(
                select(TicketWalletRecord).where(TicketWalletRecord.user_id == user_id)
            )
            count_specs = {
                "workspaces": (WorkspaceRecord, [WorkspaceRecord.deleted_at.is_(None)]),
                "projects": (ProjectRecord, [ProjectRecord.deleted_at.is_(None)]),
                "series": (SeriesRecord, [SeriesRecord.deleted_at.is_(None)]),
                "assets": (AssetRecord, [AssetRecord.deleted_at.is_(None)]),
                "media": (MediaObjectRecord, [MediaObjectRecord.deleted_at.is_(None)]),
                "tasks": (AITaskRecord, []),
                "usage": (UsageEventRecord, []),
                "orders": (ManualRechargeOrderRecord, []),
            }
            counts = {
                name: int(
                    session.scalar(
                        select(func.count(model.id)).where(model.user_id == user_id, *extra)
                    )
                    or 0
                )
                for name, (model, extra) in count_specs.items()
            }
            task_exceptions = int(
                session.scalar(
                    select(func.count(AITaskRecord.id)).where(
                        AITaskRecord.user_id == user_id,
                        AITaskRecord.status.in_(("failed", "support_review")),
                    )
                )
                or 0
            )
            order_exceptions = int(
                session.scalar(
                    select(func.count(ManualRechargeReconciliationReportRecord.id)).where(
                        ManualRechargeReconciliationReportRecord.user_id == user_id,
                        ManualRechargeReconciliationReportRecord.status == "mismatch",
                    )
                )
                or 0
            )
            recent_tasks = list(
                session.scalars(
                    select(AITaskRecord)
                    .where(AITaskRecord.user_id == user_id)
                    .order_by(AITaskRecord.updated_at.desc(), AITaskRecord.id.desc())
                    .limit(5)
                )
            )
            recent_orders = list(
                session.scalars(
                    select(ManualRechargeOrderRecord)
                    .where(ManualRechargeOrderRecord.user_id == user_id)
                    .order_by(
                        ManualRechargeOrderRecord.updated_at.desc(),
                        ManualRechargeOrderRecord.id.desc(),
                    )
                    .limit(5)
                )
            )
            user = scope.user
            return {
                "account": {
                    "id": str(user.id),
                    "username": user.username,
                    "phone_masked": mask_phone(user.phone_canonical),
                    "status": user.status,
                    "phone_verified": user.phone_verified_at is not None,
                    "created_at": _iso(user.created_at),
                    "updated_at": _iso(user.updated_at),
                },
                "wallet": {
                    "available_microtickets": str(wallet.available_microtickets if wallet else 0),
                    "available_tickets": _tickets(wallet.available_microtickets if wallet else 0),
                    "held_microtickets": str(wallet.held_microtickets if wallet else 0),
                    "held_tickets": _tickets(wallet.held_microtickets if wallet else 0),
                    "lifetime_recharged_microtickets": str(
                        wallet.lifetime_recharged_microtickets if wallet else 0
                    ),
                    "lifetime_refunded_microtickets": str(
                        wallet.lifetime_refunded_microtickets if wallet else 0
                    ),
                },
                "counts": counts,
                "exceptions": {
                    "tasks": task_exceptions,
                    "orders": order_exceptions,
                    "total": task_exceptions + order_exceptions,
                },
                "recent_activity": {
                    "tasks": [
                        {
                            "id": str(item.id),
                            "workspace_id": str(item.workspace_id),
                            "status": item.status,
                            "capability": item.capability,
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in recent_tasks
                    ],
                    "orders": [
                        {
                            "id": str(item.id),
                            "order_number": item.order_number,
                            "status": item.status,
                            "status_zh": ORDER_STATUS_ZH[item.status],
                            "updated_at": _iso(item.updated_at),
                        }
                        for item in recent_orders
                    ],
                },
            }

    def list_resources(
        self,
        admin: AdminContext,
        *,
        user_id: int,
        workspace_id: int | None,
        resource: InspectionResource,
        offset: int,
        limit: int,
        status: str | None = None,
        asset_type: str | None = None,
    ) -> dict[str, Any]:
        self._admin_id(admin)
        with self.database.transaction(admin) as session:
            self._target_scope(session, user_id, workspace_id)
            filters: list[Any]
            model: Any
            if resource == "workspaces":
                model = WorkspaceRecord
                filters = [model.user_id == user_id]
                if workspace_id is not None:
                    filters.append(model.id == workspace_id)
            elif resource in {"projects", "series"}:
                model = ProjectRecord if resource == "projects" else SeriesRecord
                filters = [model.user_id == user_id, *self._workspace_filter(model, workspace_id)]
            elif resource == "assets":
                model = AssetRecord
                filters = [
                    model.user_id == user_id,
                    *self._workspace_filter(model, workspace_id),
                ]
                if asset_type:
                    filters.append(model.asset_type == asset_type)
            elif resource == "media":
                model = MediaObjectRecord
                filters = [
                    model.scope == "user",
                    model.user_id == user_id,
                    *self._workspace_filter(model, workspace_id),
                ]
            elif resource == "tasks":
                model = AITaskRecord
                filters = [model.user_id == user_id, *self._workspace_filter(model, workspace_id)]
                if status:
                    filters.append(model.status == status)
            elif resource == "attempts":
                model = AITaskAttemptRecord
                filters = [model.user_id == user_id, *self._workspace_filter(model, workspace_id)]
                if status:
                    filters.append(model.status == status)
            elif resource == "usage":
                model = UsageEventRecord
                filters = [model.user_id == user_id, *self._workspace_filter(model, workspace_id)]
                if status:
                    filters.append(model.outcome == status)
            elif resource == "ledger":
                model = TicketLedgerRecord
                filters = [model.user_id == user_id]
                if workspace_id is not None:
                    filters.append(model.workspace_id == workspace_id)
                if status:
                    filters.append(model.entry_type == status)
            elif resource == "orders":
                model = ManualRechargeOrderRecord
                filters = [model.user_id == user_id]
                if status:
                    filters.append(model.status == status)
            elif resource == "sessions":
                model = AuthSessionRecord
                filters = [model.user_id == user_id]
            else:
                model = AuditEventRecord
                filters = [model.target_user_id == user_id]
                if workspace_id is not None:
                    filters.append(model.workspace_id == workspace_id)
                if status:
                    filters.append(model.action.ilike(f"%{status.strip()}%"))

            total = int(session.scalar(select(func.count(model.id)).where(*filters)) or 0)
            rows = list(
                session.scalars(
                    select(model)
                    .where(*filters)
                    .order_by(model.created_at.desc(), model.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            items = [self._resource_projection(resource, row) for row in rows]
            return {"items": items, "total": total, "offset": offset, "limit": limit}

    @staticmethod
    def _resource_projection(resource: InspectionResource, row: Any) -> dict[str, Any]:
        base = {"id": str(row.id), "created_at": _iso(row.created_at)}
        if resource == "workspaces":
            return {
                **base,
                "name": row.name,
                "version": row.version,
                "updated_at": _iso(row.updated_at),
                "lifecycle": "deleted" if row.deleted_at else "active",
            }
        if resource in {"projects", "series"}:
            return {
                **base,
                "workspace_id": str(row.workspace_id),
                "title": row.title,
                "version": row.version,
                "schema_version": row.schema_version,
                "updated_at": _iso(row.updated_at),
                "lifecycle": "deleted" if row.deleted_at else "active",
                **(
                    {"series_id": str(row.series_id) if row.series_id else None}
                    if resource == "projects"
                    else {}
                ),
            }
        if resource == "assets":
            provenance = row.provenance or {}
            return {
                **base,
                "workspace_id": str(row.workspace_id),
                "project_id": str(row.project_id) if row.project_id else None,
                "series_id": str(row.series_id) if row.series_id else None,
                "asset_type": row.asset_type,
                "scope": row.scope,
                "name": row.name,
                "media_id": str(row.media_object_id) if row.media_object_id else None,
                "version": row.version,
                "lifecycle": "deleted" if row.deleted_at else "active",
                "provenance": {
                    "origin": provenance.get("origin"),
                    "source_system_scene_id": provenance.get("source_system_scene_id"),
                    "source_version": provenance.get("source_version"),
                },
                "updated_at": _iso(row.updated_at),
            }
        if resource == "media":
            return {
                **base,
                "workspace_id": str(row.workspace_id),
                "project_id": str(row.project_id) if row.project_id else None,
                "mime_type": row.mime_type,
                "size_bytes": row.size_bytes,
                "checksum_sha256": row.checksum_sha256,
                "lifecycle_state": row.lifecycle_state,
            }
        if resource == "tasks":
            return {
                **base,
                "workspace_id": str(row.workspace_id),
                "project_id": str(row.project_id) if row.project_id else None,
                "capability": row.capability,
                "status": row.status,
                "quoted_microtickets": str(row.quoted_microtickets),
                "provider_billable": row.provider_billable,
                "support_review_reason": row.support_review_reason,
                "safe_error_code": row.safe_error_code,
                "safe_error_message": row.safe_error_message,
                "model": _safe_model(row.config_snapshot),
                "updated_at": _iso(row.updated_at),
                "completed_at": _iso(row.completed_at),
            }
        if resource == "attempts":
            return {
                **base,
                "workspace_id": str(row.workspace_id),
                "task_id": str(row.task_id),
                "attempt_number": row.attempt_number,
                "status": row.status,
                "provider": row.provider,
                "provider_model_id": row.provider_model_id,
                "billable_acknowledged_at": _iso(row.billable_acknowledged_at),
                "completed_at": _iso(row.completed_at),
            }
        if resource == "usage":
            return {
                **base,
                "workspace_id": str(row.workspace_id),
                "project_id": str(row.project_id) if row.project_id else None,
                "task_id": str(row.task_id),
                "capability": row.capability,
                "outcome": row.outcome,
                "metering_tokens": str(row.metering_tokens),
                "tokens_per_ticket": str(row.tokens_per_ticket),
                "charged_microtickets": str(row.charged_microtickets),
            }
        if resource == "ledger":
            return {
                **base,
                "workspace_id": str(row.workspace_id) if row.workspace_id else None,
                "project_id": str(row.project_id) if row.project_id else None,
                "task_id": str(row.task_id) if row.task_id else None,
                "manual_recharge_order_id": (
                    str(row.manual_recharge_order_id) if row.manual_recharge_order_id else None
                ),
                "entry_type": row.entry_type,
                "amount_microtickets": str(row.amount_microtickets),
                "available_delta": str(row.available_delta),
                "held_delta": str(row.held_delta),
                "available_after": str(row.available_after),
                "held_after": str(row.held_after),
            }
        if resource == "orders":
            return {
                **base,
                "order_number": row.order_number,
                "status": row.status,
                "status_zh": ORDER_STATUS_ZH[row.status],
                "cash_amount_fen": str(row.cash_amount_fen),
                "ticket_amount_microtickets": str(row.ticket_amount_microtickets),
                "refunded_cash_fen": str(row.refunded_cash_fen),
                "refunded_microtickets": str(row.refunded_microtickets),
                "version": row.version,
                "updated_at": _iso(row.updated_at),
            }
        if resource == "sessions":
            return {
                **base,
                "last_seen_at": _iso(row.last_seen_at),
                "idle_expires_at": _iso(row.idle_expires_at),
                "absolute_expires_at": _iso(row.absolute_expires_at),
                "revoked_at": _iso(row.revoked_at),
                "user_agent": row.user_agent,
            }
        return {
            **base,
            "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
            "workspace_id": str(row.workspace_id) if row.workspace_id else None,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "purpose": row.reason,
            "correlation_id": row.correlation_id,
        }

    def task_detail(
        self,
        admin: AdminContext,
        *,
        user_id: int,
        workspace_id: int,
        task_id: int,
    ) -> dict[str, Any]:
        self._admin_id(admin)
        with self.database.transaction(admin) as session:
            self._target_scope(session, user_id, workspace_id)
            task = session.scalar(
                select(AITaskRecord).where(
                    AITaskRecord.id == task_id,
                    AITaskRecord.user_id == user_id,
                    AITaskRecord.workspace_id == workspace_id,
                )
            )
            if task is None:
                raise AdminInspectionNotFoundError("AI 任务不存在")
            attempts = list(
                session.scalars(
                    select(AITaskAttemptRecord)
                    .where(
                        AITaskAttemptRecord.task_id == task_id,
                        AITaskAttemptRecord.user_id == user_id,
                        AITaskAttemptRecord.workspace_id == workspace_id,
                    )
                    .order_by(AITaskAttemptRecord.attempt_number.asc(), AITaskAttemptRecord.id.asc())
                )
            )
            holds = list(
                session.scalars(
                    select(TicketHoldRecord)
                    .where(
                        TicketHoldRecord.task_id == task_id,
                        TicketHoldRecord.user_id == user_id,
                        TicketHoldRecord.workspace_id == workspace_id,
                    )
                    .order_by(TicketHoldRecord.id.asc())
                )
            )
            usage = list(
                session.scalars(
                    select(UsageEventRecord)
                    .where(
                        UsageEventRecord.task_id == task_id,
                        UsageEventRecord.user_id == user_id,
                        UsageEventRecord.workspace_id == workspace_id,
                    )
                    .order_by(UsageEventRecord.id.asc())
                )
            )
            ledger = list(
                session.scalars(
                    select(TicketLedgerRecord)
                    .where(
                        TicketLedgerRecord.task_id == task_id,
                        TicketLedgerRecord.user_id == user_id,
                        TicketLedgerRecord.workspace_id == workspace_id,
                    )
                    .order_by(TicketLedgerRecord.id.asc())
                )
            )
            return {
                "task": self._resource_projection("tasks", task),
                "attempts": [self._resource_projection("attempts", item) for item in attempts],
                "holds": [
                    {
                        "id": str(item.id),
                        "attempt_id": str(item.attempt_id) if item.attempt_id else None,
                        "quoted_microtickets": str(item.quoted_microtickets),
                        "remaining_microtickets": str(item.remaining_microtickets),
                        "status": item.status,
                        "created_at": _iso(item.created_at),
                        "settled_at": _iso(item.settled_at),
                        "released_at": _iso(item.released_at),
                    }
                    for item in holds
                ],
                "usage": [self._resource_projection("usage", item) for item in usage],
                "ledger": [self._resource_projection("ledger", item) for item in ledger],
                "result_media_ids": _extract_media_ids(task.result),
            }

    def sensitive_script(
        self,
        admin: AdminContext,
        *,
        user_id: int,
        workspace_id: int,
        project_id: int,
        purpose: str,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        actor_id = self._admin_id(admin)
        try:
            normalized_purpose = normalize_chinese_reason(purpose, field_name="查看用途")
        except ValueError as exc:
            raise AdminInspectionValidationError(str(exc), code="SENSITIVE_PURPOSE_REQUIRED") from exc
        result: dict[str, Any]
        with self.database.transaction(admin) as session:
            self._target_scope(session, user_id, workspace_id)
            project = session.scalar(
                select(ProjectRecord).where(
                    ProjectRecord.id == project_id,
                    ProjectRecord.user_id == user_id,
                    ProjectRecord.workspace_id == workspace_id,
                    ProjectRecord.deleted_at.is_(None),
                )
            )
            if project is None:
                raise AdminInspectionNotFoundError("剧本不存在")
            body = str((project.payload or {}).get("original_text") or "")
            bounded = body[: self.SENSITIVE_TEXT_LIMIT]
            self._audit_sensitive(
                session,
                actor_id=actor_id,
                user_id=user_id,
                workspace_id=workspace_id,
                action="admin.sensitive.script.view",
                target_type="project_script",
                target_id=project_id,
                purpose=normalized_purpose,
                correlation_id=correlation_id,
            )
            result = {
                "project_id": str(project.id),
                "title": project.title,
                "text": bounded,
                "truncated": len(body) > len(bounded),
                "purpose": normalized_purpose,
            }
        return result

    def sensitive_task_prompt(
        self,
        admin: AdminContext,
        *,
        user_id: int,
        workspace_id: int,
        task_id: int,
        purpose: str,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        actor_id = self._admin_id(admin)
        try:
            normalized_purpose = normalize_chinese_reason(purpose, field_name="查看用途")
        except ValueError as exc:
            raise AdminInspectionValidationError(str(exc), code="SENSITIVE_PURPOSE_REQUIRED") from exc
        with self.database.transaction(admin) as session:
            self._target_scope(session, user_id, workspace_id)
            task = session.scalar(
                select(AITaskRecord).where(
                    AITaskRecord.id == task_id,
                    AITaskRecord.user_id == user_id,
                    AITaskRecord.workspace_id == workspace_id,
                )
            )
            if task is None:
                raise AdminInspectionNotFoundError("AI 任务不存在")
            request_payload = task.request_payload or {}
            allowed_text = {
                key: str(request_payload[key])[:20_000]
                for key in ("prompt", "negative_prompt", "text", "script")
                if key in request_payload and isinstance(request_payload[key], (str, int, float))
            }
            attempts = list(
                session.scalars(
                    select(AITaskAttemptRecord)
                    .where(
                        AITaskAttemptRecord.task_id == task_id,
                        AITaskAttemptRecord.user_id == user_id,
                        AITaskAttemptRecord.workspace_id == workspace_id,
                    )
                    .order_by(AITaskAttemptRecord.id.asc())
                )
            )
            self._audit_sensitive(
                session,
                actor_id=actor_id,
                user_id=user_id,
                workspace_id=workspace_id,
                action="admin.sensitive.task_prompt.view",
                target_type="ai_task",
                target_id=task_id,
                purpose=normalized_purpose,
                correlation_id=correlation_id,
            )
            result = {
                "task_id": str(task.id),
                "request_text": allowed_text,
                "diagnostics": [
                    {
                        "attempt_id": str(item.id),
                        "status": item.status,
                        "provider": item.provider,
                        "provider_model_id": item.provider_model_id,
                        "billable_acknowledged_at": _iso(item.billable_acknowledged_at),
                    }
                    for item in attempts
                ],
                "purpose": normalized_purpose,
            }
        return result

    def media_preview(
        self,
        admin: AdminContext,
        *,
        user_id: int,
        workspace_id: int,
        media_id: int,
        purpose: str,
        correlation_id: str | None,
        expires_seconds: int = 300,
    ) -> dict[str, Any]:
        actor_id = self._admin_id(admin)
        if self.media_storage is None:
            raise MediaStorageError("私有媒体存储尚未就绪")
        try:
            normalized_purpose = normalize_chinese_reason(purpose, field_name="预览用途")
        except ValueError as exc:
            raise AdminInspectionValidationError(str(exc), code="SENSITIVE_PURPOSE_REQUIRED") from exc
        if expires_seconds < 1 or expires_seconds > 900:
            raise AdminInspectionValidationError("预览链接有效期必须在 15 分钟以内")
        url = ""
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_seconds)
        with self.database.transaction(admin) as session:
            self._target_scope(session, user_id, workspace_id)
            media = session.scalar(
                select(MediaObjectRecord).where(
                    MediaObjectRecord.id == media_id,
                    MediaObjectRecord.scope == "user",
                    MediaObjectRecord.user_id == user_id,
                    MediaObjectRecord.workspace_id == workspace_id,
                    MediaObjectRecord.lifecycle_state == "active",
                    MediaObjectRecord.deleted_at.is_(None),
                )
            )
            if media is None:
                raise AdminInspectionNotFoundError("媒体不存在或不可预览")
            self._audit_sensitive(
                session,
                actor_id=actor_id,
                user_id=user_id,
                workspace_id=workspace_id,
                action="admin.sensitive.media.preview",
                target_type="media_object",
                target_id=media_id,
                purpose=normalized_purpose,
                correlation_id=correlation_id,
            )
            try:
                url = self.media_storage.object_store.signed_get_url(
                    media.object_key,
                    expires_seconds,
                )
            except Exception as exc:
                raise MediaStorageError("媒体预览链接生成失败") from exc
            if not url:
                raise MediaStorageError("媒体预览链接生成失败")
        return {
            "media_id": str(media_id),
            "url": url,
            "expires_at": expires_at.isoformat(),
            "disposition": "inline",
            "purpose": normalized_purpose,
        }

    @staticmethod
    def _exception_items(session: Any, *, limit: int) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        items: list[dict[str, Any]] = []
        tasks = list(
            session.scalars(
                select(AITaskRecord)
                .where(
                    or_(
                        AITaskRecord.status.in_(("failed", "support_review")),
                        and_(
                            AITaskRecord.status.in_(("reserved", "queued", "running")),
                            AITaskRecord.updated_at < now - AdminInspectionService.STALLED_AFTER,
                        ),
                    )
                )
                .order_by(AITaskRecord.updated_at.desc(), AITaskRecord.id.desc())
                .limit(limit)
            )
        )
        for task in tasks:
            kind = "support_review" if task.status == "support_review" else (
                "failed_task" if task.status == "failed" else "stalled_task"
            )
            items.append(
                {
                    "key": f"{kind}:task:{task.id}",
                    "kind": kind,
                    "severity": "high" if kind == "support_review" else "warning",
                    "user_id": str(task.user_id),
                    "workspace_id": str(task.workspace_id),
                    "resource_type": "ai_task",
                    "resource_id": str(task.id),
                    "summary": task.safe_error_message or task.support_review_reason or "AI 任务需要处理",
                    "updated_at": _iso(task.updated_at),
                }
            )
        stale_holds = list(
            session.execute(
                select(TicketHoldRecord, AITaskRecord.status)
                .outerjoin(AITaskRecord, AITaskRecord.id == TicketHoldRecord.task_id)
                .where(
                    TicketHoldRecord.status == "held",
                    TicketHoldRecord.remaining_microtickets > 0,
                    or_(
                        AITaskRecord.id.is_(None),
                        AITaskRecord.status.in_(
                            ("succeeded", "failed", "cancelled", "support_review")
                        ),
                    ),
                )
                .order_by(TicketHoldRecord.created_at.desc(), TicketHoldRecord.id.desc())
                .limit(limit)
            ).all()
        )
        for hold, task_status in stale_holds:
            missing_task = task_status is None
            items.append(
                {
                    "key": f"stale_hold:ticket_hold:{hold.id}",
                    "kind": "stale_hold",
                    "severity": "high" if missing_task else "warning",
                    "user_id": str(hold.user_id),
                    "workspace_id": str(hold.workspace_id),
                    "resource_type": "ticket_hold",
                    "resource_id": str(hold.id),
                    "summary": (
                        "算力券预扣关联的 AI 任务不存在"
                        if missing_task
                        else "终态 AI 任务仍保留未结算的算力券预扣"
                    ),
                    "updated_at": _iso(hold.created_at),
                }
            )
        reports = list(
            session.scalars(
                select(ManualRechargeReconciliationReportRecord)
                .where(ManualRechargeReconciliationReportRecord.status == "mismatch")
                .order_by(
                    ManualRechargeReconciliationReportRecord.created_at.desc(),
                    ManualRechargeReconciliationReportRecord.id.desc(),
                )
                .limit(limit)
            )
        )
        seen_orders: set[int | None] = set()
        for report in reports:
            if report.order_id in seen_orders:
                continue
            seen_orders.add(report.order_id)
            items.append(
                {
                    "key": f"order_mismatch:order:{report.order_id or 0}",
                    "kind": "order_mismatch",
                    "severity": report.severity,
                    "user_id": str(report.user_id) if report.user_id else None,
                    "workspace_id": None,
                    "resource_type": "manual_recharge_order",
                    "resource_id": str(report.order_id) if report.order_id else None,
                    "summary": "人工充值订单与账本对账不一致",
                    "updated_at": _iso(report.created_at),
                }
            )
        missing_media = list(
            session.execute(
                select(AssetRecord.id, AssetRecord.user_id, AssetRecord.workspace_id, AssetRecord.updated_at)
                .outerjoin(MediaObjectRecord, MediaObjectRecord.id == AssetRecord.media_object_id)
                .where(
                    AssetRecord.scope != "system",
                    AssetRecord.deleted_at.is_(None),
                    AssetRecord.media_object_id.is_not(None),
                    MediaObjectRecord.id.is_(None),
                )
                .order_by(AssetRecord.updated_at.desc(), AssetRecord.id.desc())
                .limit(limit)
            ).all()
        )
        for asset_id, user_id, workspace_id, updated_at in missing_media:
            items.append(
                {
                    "key": f"missing_media:asset:{asset_id}",
                    "kind": "missing_media",
                    "severity": "high",
                    "user_id": str(user_id),
                    "workspace_id": str(workspace_id),
                    "resource_type": "asset",
                    "resource_id": str(asset_id),
                    "summary": "资产引用的媒体记录不存在",
                    "updated_at": _iso(updated_at),
                }
            )
        items.sort(key=lambda item: (item["updated_at"] or "", item["key"]), reverse=True)
        return items[:limit]

    def exceptions(self, admin: AdminContext, *, limit: int = 100) -> dict[str, Any]:
        self._admin_id(admin)
        with self.database.transaction(admin) as session:
            items = self._exception_items(session, limit=limit)
            return {"items": items, "total": len(items), "generated_at": datetime.now(UTC).isoformat()}

    def dashboard(
        self,
        admin: AdminContext,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, Any]:
        self._admin_id(admin)
        try:
            start_at, end_at = validate_bounded_window(start_at, end_at)
        except ValueError as exc:
            raise AdminInspectionValidationError(str(exc), code="DASHBOARD_WINDOW_INVALID") from exc
        with self.database.transaction(admin) as session:
            users_total = int(session.scalar(select(func.count(UserRecord.id))) or 0)
            users_new = int(
                session.scalar(
                    select(func.count(UserRecord.id)).where(
                        UserRecord.created_at >= start_at,
                        UserRecord.created_at < end_at,
                    )
                )
                or 0
            )
            order_totals = session.execute(
                select(
                    func.count(ManualRechargeOrderRecord.id),
                    func.coalesce(
                        func.sum(
                            ManualRechargeOrderRecord.cash_amount_fen
                            - ManualRechargeOrderRecord.refunded_cash_fen
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            ManualRechargeOrderRecord.ticket_amount_microtickets
                            - ManualRechargeOrderRecord.refunded_microtickets
                        ),
                        0,
                    ),
                ).where(
                    ManualRechargeOrderRecord.created_at >= start_at,
                    ManualRechargeOrderRecord.created_at < end_at,
                    ManualRechargeOrderRecord.status.in_(
                        ("completed", "partially_refunded", "refunded")
                    ),
                )
            ).one()
            usage_totals = session.execute(
                select(
                    func.count(UsageEventRecord.id),
                    func.coalesce(func.sum(UsageEventRecord.metering_tokens), 0),
                    func.coalesce(func.sum(UsageEventRecord.charged_microtickets), 0),
                ).where(
                    UsageEventRecord.created_at >= start_at,
                    UsageEventRecord.created_at < end_at,
                )
            ).one()
            task_rows = session.execute(
                select(AITaskRecord.status, func.count(AITaskRecord.id))
                .where(AITaskRecord.created_at >= start_at, AITaskRecord.created_at < end_at)
                .group_by(AITaskRecord.status)
            ).all()
            task_counts = {status: int(count) for status, count in task_rows}
            exceptions = self._exception_items(session, limit=20)
            return {
                "generated_at": datetime.now(UTC).isoformat(),
                "window": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()},
                "users": {"total": users_total, "new": users_new},
                "orders": {
                    "paid_count": int(order_totals[0]),
                    "net_cash_fen": str(order_totals[1]),
                    "net_ticket_microtickets": str(order_totals[2]),
                },
                "usage": {
                    "events": int(usage_totals[0]),
                    "metering_tokens": str(usage_totals[1]),
                    "charged_microtickets": str(usage_totals[2]),
                },
                "tasks": {
                    "total": sum(task_counts.values()),
                    "by_status": task_counts,
                    "support_review": task_counts.get("support_review", 0),
                    "failed": task_counts.get("failed", 0),
                },
                "exceptions": {"total": len(exceptions), "items": exceptions},
            }

    @staticmethod
    def _csv_safe(value: Any) -> str:
        text = "" if value is None else str(value)
        if text.startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    def export_csv(
        self,
        admin: AdminContext,
        *,
        resource: ExportResource,
        start_at: datetime,
        end_at: datetime,
        user_id: int | None,
        status: str | None,
        purpose: str,
        correlation_id: str | None,
        row_cap: int = ADMIN_EXPORT_ROW_CAP,
    ) -> str:
        actor_id = self._admin_id(admin)
        try:
            start_at, end_at = validate_bounded_window(start_at, end_at)
            normalized_purpose = normalize_chinese_reason(purpose, field_name="导出用途")
        except ValueError as exc:
            raise AdminInspectionValidationError(str(exc), code="EXPORT_FILTER_INVALID") from exc
        if row_cap < 1 or row_cap > ADMIN_EXPORT_ROW_CAP:
            raise AdminInspectionValidationError("导出行数上限无效")
        with self.database.transaction(admin) as session:
            if user_id is not None:
                self._target_scope(session, user_id, None)
            columns, rows = self._export_rows(
                session,
                resource=resource,
                start_at=start_at,
                end_at=end_at,
                user_id=user_id,
                status=status,
                limit=row_cap + 1,
            )
            if len(rows) > row_cap:
                raise AdminInspectionValidationError(
                    f"导出结果超过 {row_cap} 行，请缩小筛选范围",
                    code="EXPORT_ROW_CAP_EXCEEDED",
                )
            session.add(
                AuditEventRecord(
                    actor_admin_id=actor_id,
                    target_user_id=user_id,
                    action=f"admin.export.{resource}",
                    target_type="admin_export",
                    target_id=None,
                    reason=normalized_purpose,
                    after_summary={
                        "resource": resource,
                        "row_count": len(rows),
                        "start_at": start_at.isoformat(),
                        "end_at": end_at.isoformat(),
                        "status": status,
                    },
                    correlation_id=correlation_id or str(uuid.uuid4()),
                )
            )
            session.flush()
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([self._csv_safe(value) for value in row])
            content = "\ufeff" + output.getvalue()
        return content

    @staticmethod
    def _export_rows(
        session: Any,
        *,
        resource: ExportResource,
        start_at: datetime,
        end_at: datetime,
        user_id: int | None,
        status: str | None,
        limit: int,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        if resource == "users":
            filters = [UserRecord.created_at >= start_at, UserRecord.created_at < end_at]
            if user_id is not None:
                filters.append(UserRecord.id == user_id)
            if status:
                filters.append(UserRecord.status == status)
            records = list(
                session.execute(
                    select(UserRecord, TicketWalletRecord)
                    .outerjoin(TicketWalletRecord, TicketWalletRecord.user_id == UserRecord.id)
                    .where(*filters)
                    .order_by(UserRecord.created_at.desc(), UserRecord.id.desc())
                    .limit(limit)
                ).all()
            )
            return ["用户ID", "账号", "手机号", "状态", "可用票数", "冻结票数", "注册时间"], [
                (
                    user.id,
                    user.username or "",
                    mask_phone(user.phone_canonical) or "",
                    user.status,
                    _tickets(wallet.available_microtickets if wallet else 0),
                    _tickets(wallet.held_microtickets if wallet else 0),
                    _iso(user.created_at),
                )
                for user, wallet in records
            ]
        if resource == "orders":
            filters = [
                ManualRechargeOrderRecord.created_at >= start_at,
                ManualRechargeOrderRecord.created_at < end_at,
            ]
            if user_id is not None:
                filters.append(ManualRechargeOrderRecord.user_id == user_id)
            if status:
                filters.append(ManualRechargeOrderRecord.status == status)
            records = list(
                session.scalars(
                    select(ManualRechargeOrderRecord)
                    .where(*filters)
                    .order_by(
                        ManualRechargeOrderRecord.created_at.desc(),
                        ManualRechargeOrderRecord.id.desc(),
                    )
                    .limit(limit)
                )
            )
            return ["订单ID", "订单号", "用户ID", "状态", "金额分", "票数微单位", "退款金额分", "退款票数微单位", "创建时间"], [
                (
                    row.id,
                    row.order_number,
                    row.user_id,
                    ORDER_STATUS_ZH[row.status],
                    row.cash_amount_fen,
                    row.ticket_amount_microtickets,
                    row.refunded_cash_fen,
                    row.refunded_microtickets,
                    _iso(row.created_at),
                )
                for row in records
            ]
        if resource == "usage":
            filters = [UsageEventRecord.created_at >= start_at, UsageEventRecord.created_at < end_at]
            if user_id is not None:
                filters.append(UsageEventRecord.user_id == user_id)
            if status:
                filters.append(UsageEventRecord.outcome == status)
            records = list(
                session.scalars(
                    select(UsageEventRecord)
                    .where(*filters)
                    .order_by(UsageEventRecord.created_at.desc(), UsageEventRecord.id.desc())
                    .limit(limit)
                )
            )
            return ["用量ID", "用户ID", "工作区ID", "任务ID", "能力", "结果", "计量Token", "消耗微票", "时间"], [
                (
                    row.id,
                    row.user_id,
                    row.workspace_id,
                    row.task_id,
                    row.capability,
                    row.outcome,
                    row.metering_tokens,
                    row.charged_microtickets,
                    _iso(row.created_at),
                )
                for row in records
            ]
        filters = [AITaskRecord.created_at >= start_at, AITaskRecord.created_at < end_at]
        if user_id is not None:
            filters.append(AITaskRecord.user_id == user_id)
        if status:
            filters.append(AITaskRecord.status == status)
        records = list(
            session.scalars(
                select(AITaskRecord)
                .where(*filters)
                .order_by(AITaskRecord.created_at.desc(), AITaskRecord.id.desc())
                .limit(limit)
            )
        )
        return ["任务ID", "用户ID", "工作区ID", "项目ID", "能力", "状态", "模型", "预估微票", "创建时间"], [
            (
                row.id,
                row.user_id,
                row.workspace_id,
                row.project_id or "",
                row.capability,
                row.status,
                _safe_model(row.config_snapshot)["display_name"],
                row.quoted_microtickets,
                _iso(row.created_at),
            )
            for row in records
        ]
