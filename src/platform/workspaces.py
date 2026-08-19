from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select

from .admin_access import require_platform_admin_context
from .contracts import AdminContext, UserContext
from .database import Database
from .db_models import (
    AITaskRecord,
    AssetRecord,
    ImportBatchRecord,
    MediaObjectRecord,
    ProjectRecord,
    SeriesRecord,
    WorkspaceRecord,
)
from .identifiers import parse_database_id


class WorkspaceNotFoundError(LookupError):
    pass


class WorkspaceConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceCleanupResult:
    deleted: int
    skipped: int


class WorkspaceService:
    RETENTION_DAYS = 30

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _name(value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("工作区名称不能为空")
        if len(name) > 120:
            raise ValueError("工作区名称不能超过 120 个字符")
        return name

    def create(self, identity: UserContext, name: str) -> WorkspaceRecord:
        record = WorkspaceRecord(
            user_id=parse_database_id(identity.user_id, field="用户 ID"),
            name=self._name(name),
        )
        with self.database.transaction(identity) as session:
            session.add(record)
            session.flush()
        return record

    def list(self, identity: UserContext, *, include_deleted: bool = False) -> list[WorkspaceRecord]:
        statement = select(WorkspaceRecord).where(
            WorkspaceRecord.user_id == parse_database_id(identity.user_id, field="用户 ID")
        )
        if not include_deleted:
            statement = statement.where(WorkspaceRecord.deleted_at.is_(None))
        with self.database.transaction(identity) as session:
            return list(session.scalars(statement.order_by(WorkspaceRecord.created_at.asc())))

    def select(self, identity: UserContext, workspace_id: int) -> WorkspaceRecord:
        with self.database.transaction(identity) as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.user_id == parse_database_id(identity.user_id, field="用户 ID"),
                    WorkspaceRecord.deleted_at.is_(None),
                )
            )
            if record is None:
                raise WorkspaceNotFoundError("工作区不存在")
            session.expunge(record)
            return record

    def rename(
        self,
        identity: UserContext,
        workspace_id: int,
        name: str,
        expected_version: int,
    ) -> WorkspaceRecord:
        with self.database.transaction(identity) as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.user_id == parse_database_id(identity.user_id, field="用户 ID"),
                    WorkspaceRecord.deleted_at.is_(None),
                )
            )
            if record is None:
                raise WorkspaceNotFoundError("工作区不存在")
            if record.version != expected_version:
                raise WorkspaceConflictError("工作区已在其他位置更新，请刷新后重试")
            record.name = self._name(name)
            record.version += 1
            record.updated_at = datetime.now(UTC)
            session.flush()
            session.expunge(record)
            return record

    def soft_delete(
        self,
        identity: UserContext,
        workspace_id: int,
        *,
        now: datetime | None = None,
    ) -> None:
        deleted_at = now or datetime.now(UTC)
        with self.database.transaction(identity) as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.user_id == parse_database_id(identity.user_id, field="用户 ID"),
                    WorkspaceRecord.deleted_at.is_(None),
                )
            )
            if record is None:
                raise WorkspaceNotFoundError("工作区不存在")
            active_count = session.scalar(
                select(WorkspaceRecord.id)
                .where(
                    WorkspaceRecord.user_id == record.user_id,
                    WorkspaceRecord.deleted_at.is_(None),
                    WorkspaceRecord.id != workspace_id,
                )
                .limit(1)
            )
            if active_count is None:
                raise WorkspaceConflictError("至少需要保留一个可用工作区")
            record.deleted_at = deleted_at
            record.retention_expires_at = deleted_at + timedelta(days=self.RETENTION_DAYS)
            record.version += 1

    def restore(
        self,
        identity: UserContext,
        workspace_id: int,
        *,
        now: datetime | None = None,
    ) -> WorkspaceRecord:
        restored_at = now or datetime.now(UTC)
        with self.database.transaction(identity) as session:
            record = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.user_id == parse_database_id(identity.user_id, field="用户 ID"),
                    WorkspaceRecord.deleted_at.is_not(None),
                )
            )
            if record is None:
                raise WorkspaceNotFoundError("工作区不存在")
            if record.retention_expires_at and restored_at >= record.retention_expires_at:
                raise WorkspaceConflictError("工作区恢复期限已过")
            record.deleted_at = None
            record.retention_expires_at = None
            record.version += 1
            record.updated_at = restored_at
            session.flush()
            session.expunge(record)
            return record

    def cleanup_expired(
        self,
        admin: AdminContext,
        *,
        now: datetime | None = None,
    ) -> WorkspaceCleanupResult:
        require_platform_admin_context(admin)
        checked_at = now or datetime.now(UTC)
        deleted_count = 0
        skipped_count = 0
        with self.database.transaction(admin) as session:
            records = list(
                session.scalars(
                    select(WorkspaceRecord).where(
                        WorkspaceRecord.deleted_at.is_not(None),
                        WorkspaceRecord.retention_expires_at <= checked_at,
                    )
                )
            )
            for record in records:
                has_history = session.scalar(
                    select(
                        exists().where(
                            (AITaskRecord.user_id == record.user_id)
                            & (AITaskRecord.workspace_id == record.id)
                        )
                        | exists().where(
                            (ImportBatchRecord.target_user_id == record.user_id)
                            & (ImportBatchRecord.target_workspace_id == record.id)
                        )
                    )
                )
                if has_history:
                    skipped_count += 1
                    continue
                owner_filter = {
                    "user_id": record.user_id,
                    "workspace_id": record.id,
                }
                session.execute(delete(AssetRecord).filter_by(**owner_filter))
                session.execute(delete(MediaObjectRecord).filter_by(**owner_filter))
                session.execute(delete(ProjectRecord).filter_by(**owner_filter))
                session.execute(delete(SeriesRecord).filter_by(**owner_filter))
                session.delete(record)
                deleted_count += 1
        return WorkspaceCleanupResult(deleted=deleted_count, skipped=skipped_count)
