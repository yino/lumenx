from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import delete, exists, or_, select, update

from .ai_dispatch import CeleryRecoveryDispatcher
from .ai_recovery import AIRecoveryRepository
from .contracts import SystemContext
from .database import Database
from .db_models import (
    AITaskRecord,
    AssetRecord,
    AuthSessionRecord,
    ConfigVersionRecord,
    ImportBatchRecord,
    ImportedPlaygroundHistoryRecord,
    MediaObjectRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
    ProjectRecord,
    SeriesRecord,
    WorkspaceRecord,
)
from .media_storage import PrivateObjectStore
from .observability import events, metrics
from .runtime_policy import RuntimePolicyResolver
from .ticket_reconciliation import TicketReconciliationService


MAINTENANCE_QUEUE = "maintenance"
SESSION_MAINTENANCE_TASK = "lumenx.maintenance.expire_sessions"
RETENTION_MAINTENANCE_TASK = "lumenx.maintenance.cleanup_retention"
MEDIA_MAINTENANCE_TASK = "lumenx.maintenance.cleanup_media"
RECONCILIATION_MAINTENANCE_TASK = "lumenx.maintenance.reconcile_tasks_holds"
BACKUP_MAINTENANCE_TASK = "lumenx.maintenance.verify_backup"

MAINTENANCE_IDENTITY = SystemContext(service_name="scheduled-maintenance")


class MaintenanceAuthorizationError(PermissionError):
    pass


class BackupVerificationError(RuntimeError):
    pass


class RecoveryDispatcher(Protocol):
    def dispatch(self, task_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionExpiryReport:
    checked_at: datetime
    revoked_sessions: int


@dataclass(frozen=True, slots=True)
class RetentionCleanupReport:
    checked_at: datetime
    deleted_assets: int
    deleted_projects: int
    deleted_series: int
    deleted_playground_history: int
    deleted_workspaces: int
    skipped_workspaces: int
    policy_config_version_id: str | None = None
    retention_days: int | None = None


@dataclass(frozen=True, slots=True)
class OrphanMediaCleanupReport:
    checked_at: datetime
    candidates: int
    deleted: int
    failed: int
    policy_config_version_id: str | None = None
    retention_days: int | None = None


@dataclass(frozen=True, slots=True)
class TaskHoldReconciliationReport:
    checked_at: datetime
    recovery_candidates: int
    recovery_dispatched: int
    recovery_dispatch_failed: int
    reconciliation_consistent: bool
    reconciliation_issues: int
    policy_config_version_id: str | None = None
    stale_hold_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class BackupVerificationReport:
    checked_at: datetime
    backup_file: str
    size_bytes: int
    sha256: str
    age_seconds: int
    required_tables: tuple[str, ...]


def _require_admin(identity: SystemContext) -> None:
    if not isinstance(identity, SystemContext):
        raise MaintenanceAuthorizationError("仅平台管理员维护身份可以执行定时维护")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SessionExpiryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def run(
        self,
        identity: SystemContext = MAINTENANCE_IDENTITY,
        *,
        now: datetime | None = None,
    ) -> SessionExpiryReport:
        _require_admin(identity)
        checked_at = _utc(now or datetime.now(UTC))
        with self.database.transaction(identity) as session:
            result = session.execute(
                update(AuthSessionRecord)
                .where(
                    AuthSessionRecord.revoked_at.is_(None),
                    or_(
                        AuthSessionRecord.idle_expires_at <= checked_at,
                        AuthSessionRecord.absolute_expires_at <= checked_at,
                    ),
                )
                .values(revoked_at=checked_at)
            )
            revoked = int(result.rowcount or 0)
        metrics.increment(
            "maintenance_records_total",
            revoked,
            labels={"operation": "expire_sessions", "outcome": "revoked"},
        )
        return SessionExpiryReport(checked_at=checked_at, revoked_sessions=revoked)


class RetentionCleanupService:
    def __init__(
        self,
        database: Database,
        object_store: PrivateObjectStore,
        *,
        runtime_policy: RuntimePolicyResolver | None = None,
    ) -> None:
        self.database = database
        self.object_store = object_store
        self.runtime_policy = runtime_policy

    def _delete_objects(self, object_keys: tuple[str, ...]) -> bool:
        try:
            for object_key in object_keys:
                self.object_store.delete(object_key)
        except Exception as exc:
            events.emit(
                "maintenance.retention_object_delete_failed",
                operation="retention",
                error_type=type(exc).__name__,
            )
            return False
        return True

    def run(
        self,
        identity: SystemContext = MAINTENANCE_IDENTITY,
        *,
        now: datetime | None = None,
    ) -> RetentionCleanupReport:
        _require_admin(identity)
        checked_at = _utc(now or datetime.now(UTC))
        policy = self.runtime_policy.resolve() if self.runtime_policy else None
        retention_days = policy.soft_delete_retention_days if policy else None
        retention_cutoff = (
            checked_at - timedelta(days=retention_days)
            if retention_days is not None
            else None
        )
        def expired_condition(record_type: Any) -> Any:
            if retention_cutoff is not None:
                return record_type.deleted_at <= retention_cutoff
            return record_type.retention_expires_at <= checked_at
        deleted_assets = 0
        deleted_projects = 0
        deleted_series = 0
        deleted_history = 0
        deleted_workspaces = 0
        skipped_workspaces = 0

        with self.database.transaction(identity) as session:
            result = session.execute(
                delete(ImportedPlaygroundHistoryRecord).where(
                    ImportedPlaygroundHistoryRecord.deleted_at.is_not(None),
                    expired_condition(ImportedPlaygroundHistoryRecord),
                )
            )
            deleted_history = int(result.rowcount or 0)
            result = session.execute(
                delete(AssetRecord).where(
                    AssetRecord.deleted_at.is_not(None),
                    expired_condition(AssetRecord),
                )
            )
            deleted_assets = int(result.rowcount or 0)

        with self.database.transaction(identity) as session:
            project_ids = tuple(
                session.scalars(
                    select(ProjectRecord.id).where(
                        ProjectRecord.deleted_at.is_not(None),
                        expired_condition(ProjectRecord),
                        ~exists().where(AITaskRecord.project_id == ProjectRecord.id),
                        ~exists().where(MediaObjectRecord.project_id == ProjectRecord.id),
                    )
                )
            )
            if project_ids:
                result = session.execute(delete(ProjectRecord).where(ProjectRecord.id.in_(project_ids)))
                deleted_projects = int(result.rowcount or 0)

            series_ids = tuple(
                session.scalars(
                    select(SeriesRecord.id).where(
                        SeriesRecord.deleted_at.is_not(None),
                        expired_condition(SeriesRecord),
                        ~exists().where(ProjectRecord.series_id == SeriesRecord.id),
                    )
                )
            )
            if series_ids:
                result = session.execute(delete(SeriesRecord).where(SeriesRecord.id.in_(series_ids)))
                deleted_series = int(result.rowcount or 0)

        with self.database.transaction(identity) as session:
            workspace_ids = tuple(
                session.scalars(
                    select(WorkspaceRecord.id).where(
                        WorkspaceRecord.deleted_at.is_not(None),
                        expired_condition(WorkspaceRecord),
                    )
                )
            )
        for workspace_id in workspace_ids:
            with self.database.transaction(identity) as session:
                workspace = session.get(WorkspaceRecord, workspace_id)
                if workspace is None:
                    continue
                has_history = bool(
                    session.scalar(
                        select(
                            exists().where(AITaskRecord.workspace_id == workspace_id)
                            | exists().where(ImportBatchRecord.target_workspace_id == workspace_id)
                        )
                    )
                )
                if has_history:
                    skipped_workspaces += 1
                    continue
                object_keys = tuple(
                    session.scalars(
                        select(MediaObjectRecord.object_key).where(
                            MediaObjectRecord.workspace_id == workspace_id
                        )
                    )
                )
            if not self._delete_objects(object_keys):
                skipped_workspaces += 1
                continue
            with self.database.transaction(identity) as session:
                workspace = session.get(WorkspaceRecord, workspace_id)
                if workspace is None:
                    continue
                session.execute(delete(AssetRecord).where(AssetRecord.workspace_id == workspace_id))
                session.execute(delete(MediaObjectRecord).where(MediaObjectRecord.workspace_id == workspace_id))
                session.execute(delete(ProjectRecord).where(ProjectRecord.workspace_id == workspace_id))
                session.execute(delete(SeriesRecord).where(SeriesRecord.workspace_id == workspace_id))
                session.delete(workspace)
                deleted_workspaces += 1

        totals = {
            "asset": deleted_assets,
            "project": deleted_projects,
            "series": deleted_series,
            "playground_history": deleted_history,
            "workspace": deleted_workspaces,
        }
        for resource, count in totals.items():
            metrics.increment(
                "maintenance_records_total",
                count,
                labels={"operation": "retention", "outcome": "deleted", "resource": resource},
            )
        return RetentionCleanupReport(
            checked_at=checked_at,
            deleted_assets=deleted_assets,
            deleted_projects=deleted_projects,
            deleted_series=deleted_series,
            deleted_playground_history=deleted_history,
            deleted_workspaces=deleted_workspaces,
            skipped_workspaces=skipped_workspaces,
            policy_config_version_id=policy.config_version_id if policy else None,
            retention_days=retention_days,
        )


class OrphanMediaCleanupService:
    def __init__(
        self,
        database: Database,
        object_store: PrivateObjectStore,
        *,
        runtime_policy: RuntimePolicyResolver | None = None,
    ) -> None:
        self.database = database
        self.object_store = object_store
        self.runtime_policy = runtime_policy

    def run(
        self,
        identity: SystemContext = MAINTENANCE_IDENTITY,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(hours=24),
        limit: int = 1000,
    ) -> OrphanMediaCleanupReport:
        _require_admin(identity)
        if stale_after <= timedelta(0) or limit < 1 or limit > 10_000:
            raise ValueError("媒体清理阈值或数量上限无效")
        checked_at = _utc(now or datetime.now(UTC))
        policy = self.runtime_policy.resolve() if self.runtime_policy else None
        retention_days = policy.soft_delete_retention_days if policy else None
        deleted_cutoff = (
            checked_at - timedelta(days=retention_days)
            if retention_days is not None
            else None
        )
        stale_before = checked_at - stale_after
        with self.database.transaction(identity) as session:
            records = tuple(
                session.execute(
                    select(MediaObjectRecord.id, MediaObjectRecord.object_key)
                    .where(
                        or_(
                            (
                                (MediaObjectRecord.lifecycle_state == "deleted")
                                & (
                                    (MediaObjectRecord.deleted_at <= deleted_cutoff)
                                    if deleted_cutoff is not None
                                    else (MediaObjectRecord.retention_expires_at <= checked_at)
                                )
                            ),
                            (
                                MediaObjectRecord.lifecycle_state.in_({"pending", "failed"})
                                & (MediaObjectRecord.created_at <= stale_before)
                            ),
                        ),
                        ~exists().where(AssetRecord.media_object_id == MediaObjectRecord.id),
                    )
                    .order_by(MediaObjectRecord.created_at, MediaObjectRecord.id)
                    .limit(limit)
                )
            )
        deleted_count = 0
        failed_count = 0
        for media_id, object_key in records:
            try:
                self.object_store.delete(object_key)
                with self.database.transaction(identity) as session:
                    result = session.execute(
                        delete(MediaObjectRecord).where(
                            MediaObjectRecord.id == media_id,
                            MediaObjectRecord.lifecycle_state.in_({"deleted", "pending", "failed"}),
                        )
                    )
                    deleted_count += int(result.rowcount or 0)
            except Exception as exc:
                failed_count += 1
                events.emit(
                    "maintenance.media_delete_failed",
                    media_id=str(media_id),
                    operation="orphan_cleanup",
                    error_type=type(exc).__name__,
                )
        metrics.increment(
            "maintenance_records_total",
            deleted_count,
            labels={"operation": "orphan_media", "outcome": "deleted", "resource": "media"},
        )
        metrics.increment(
            "maintenance_records_total",
            failed_count,
            labels={"operation": "orphan_media", "outcome": "failed", "resource": "media"},
        )
        return OrphanMediaCleanupReport(
            checked_at=checked_at,
            candidates=len(records),
            deleted=deleted_count,
            failed=failed_count,
            policy_config_version_id=policy.config_version_id if policy else None,
            retention_days=retention_days,
        )


class TaskHoldMaintenanceService:
    def __init__(
        self,
        database: Database,
        recovery_dispatcher: RecoveryDispatcher,
        *,
        recovery_stale_after_seconds: int = 300,
        runtime_policy: RuntimePolicyResolver | None = None,
    ) -> None:
        self.database = database
        self.recovery_dispatcher = recovery_dispatcher
        self.runtime_policy = runtime_policy
        self.recovery = AIRecoveryRepository(
            database,
            stale_after_seconds=recovery_stale_after_seconds,
        )

    def run(
        self,
        identity: SystemContext = MAINTENANCE_IDENTITY,
        *,
        now: datetime | None = None,
        stale_hold_after: timedelta = timedelta(minutes=30),
        recovery_limit: int = 100,
    ) -> TaskHoldReconciliationReport:
        _require_admin(identity)
        checked_at = _utc(now or datetime.now(UTC))
        policy = self.runtime_policy.resolve() if self.runtime_policy else None
        if policy is not None:
            stale_hold_after = timedelta(minutes=policy.stale_hold_minutes)
        candidates = self.recovery.list_candidate_ids(limit=recovery_limit)
        dispatched = 0
        dispatch_failed = 0
        for task_id in candidates:
            try:
                self.recovery_dispatcher.dispatch(task_id)
                dispatched += 1
            except Exception as exc:
                dispatch_failed += 1
                events.emit(
                    "maintenance.recovery_dispatch_failed",
                    task_id=task_id,
                    operation="recovery_dispatch",
                    error_type=type(exc).__name__,
                )
        reconciliation = TicketReconciliationService(self.database).reconcile(
            identity,
            stale_after=stale_hold_after,
            now=checked_at,
        )
        metrics.gauge(
            "maintenance_reconciliation_issues",
            len(reconciliation.issues),
            labels={"operation": "ticket_reconciliation"},
        )
        return TaskHoldReconciliationReport(
            checked_at=checked_at,
            recovery_candidates=len(candidates),
            recovery_dispatched=dispatched,
            recovery_dispatch_failed=dispatch_failed,
            reconciliation_consistent=reconciliation.is_consistent,
            reconciliation_issues=len(reconciliation.issues),
            policy_config_version_id=policy.config_version_id if policy else None,
            stale_hold_minutes=(policy.stale_hold_minutes if policy else None),
        )


class BackupVerificationService:
    REQUIRED_TABLES = (
        "audit_events",
        "config_versions",
        "model_configs",
        "platform_configs",
    )

    def __init__(self, backup_root: Path) -> None:
        self.backup_root = backup_root

    def run(
        self,
        identity: SystemContext = MAINTENANCE_IDENTITY,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(hours=36),
    ) -> BackupVerificationReport:
        _require_admin(identity)
        if max_age <= timedelta(0):
            raise ValueError("备份最大允许时效必须大于零")
        checked_at = _utc(now or datetime.now(UTC))
        backups = sorted(self.backup_root.glob("lumenx-*.dump"), key=lambda item: item.stat().st_mtime)
        if not backups:
            raise BackupVerificationError("未找到 PostgreSQL 备份文件")
        backup = backups[-1]
        checksum_file = backup.with_suffix(backup.suffix + ".sha256")
        catalog_file = backup.with_suffix(backup.suffix + ".list")
        if not checksum_file.is_file() or not catalog_file.is_file():
            raise BackupVerificationError("最新备份缺少校验和或目录清单")
        size_bytes = backup.stat().st_size
        if size_bytes <= 0:
            raise BackupVerificationError("最新备份文件为空")
        age_seconds = max(int((checked_at - datetime.fromtimestamp(backup.stat().st_mtime, UTC)).total_seconds()), 0)
        if age_seconds > int(max_age.total_seconds()):
            raise BackupVerificationError("最新备份已超过允许时效")
        expected_sha = checksum_file.read_text(encoding="utf-8").split()[0].strip().lower()
        actual_sha = hashlib.sha256(backup.read_bytes()).hexdigest()
        if expected_sha != actual_sha:
            raise BackupVerificationError("最新备份校验和不一致")
        catalog = catalog_file.read_text(encoding="utf-8", errors="strict")
        missing = [table for table in self.REQUIRED_TABLES if table not in catalog]
        if missing:
            raise BackupVerificationError(f"最新备份缺少审计或配置表：{', '.join(missing)}")
        metrics.gauge(
            "maintenance_backup_age_seconds",
            age_seconds,
            labels={"operation": "backup_verification"},
        )
        return BackupVerificationReport(
            checked_at=checked_at,
            backup_file=backup.name,
            size_bytes=size_bytes,
            sha256=actual_sha,
            age_seconds=age_seconds,
            required_tables=self.REQUIRED_TABLES,
        )


@dataclass(slots=True)
class PlatformMaintenance:
    sessions: SessionExpiryService
    retention: RetentionCleanupService
    media: OrphanMediaCleanupService
    reconciliation: TaskHoldMaintenanceService
    backup: BackupVerificationService

    @staticmethod
    def serialize(report: Any) -> dict[str, Any]:
        return asdict(report)


def build_platform_maintenance(
    database: Database,
    object_store: PrivateObjectStore,
    recovery_dispatcher: CeleryRecoveryDispatcher,
    *,
    backup_root: Path,
    recovery_stale_after_seconds: int = 300,
) -> PlatformMaintenance:
    runtime_policy = RuntimePolicyResolver(database)
    return PlatformMaintenance(
        sessions=SessionExpiryService(database),
        retention=RetentionCleanupService(
            database,
            object_store,
            runtime_policy=runtime_policy,
        ),
        media=OrphanMediaCleanupService(
            database,
            object_store,
            runtime_policy=runtime_policy,
        ),
        reconciliation=TaskHoldMaintenanceService(
            database,
            recovery_dispatcher,
            recovery_stale_after_seconds=recovery_stale_after_seconds,
            runtime_policy=runtime_policy,
        ),
        backup=BackupVerificationService(backup_root),
    )
