from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.apps.comic_gen.models import Script, Series

from .contracts import UserContext, VersionedDocument, WorkspaceContext
from .database import Database
from .db_models import ProjectRecord, SeriesRecord, WorkspaceRecord
from .identifiers import parse_database_id


DocumentT = TypeVar("DocumentT", bound=BaseModel)


class InvalidRepositoryContextError(ValueError):
    pass


class DocumentPayloadValidationError(ValueError):
    pass


class ScopedDocumentNotFoundError(LookupError):
    pass


class OptimisticVersionConflictError(RuntimeError):
    pass


class PostgresDocumentRepository(Generic[DocumentT]):
    SCHEMA_VERSION = 1
    RETENTION_DAYS = 30

    def __init__(
        self,
        database: Database,
        *,
        record_type: type[ProjectRecord] | type[SeriesRecord],
        document_type: type[DocumentT],
    ) -> None:
        self.database = database
        self.record_type = record_type
        self.document_type = document_type

    @staticmethod
    def _context_ids(context: WorkspaceContext) -> tuple[int, int]:
        if not isinstance(context, WorkspaceContext) or not isinstance(
            context.identity, UserContext
        ):
            raise InvalidRepositoryContextError(
                "内容仓储必须包含用户和工作区上下文"
            )
        try:
            user_id = parse_database_id(context.identity.user_id, field="用户 ID")
            workspace_id = parse_database_id(context.workspace_id, field="工作区 ID")
        except (AttributeError, TypeError, ValueError) as exc:
            raise InvalidRepositoryContextError("用户或工作区标识无效") from exc
        return user_id, workspace_id

    @staticmethod
    def _resource_id(resource_id: str) -> int | None:
        try:
            return parse_database_id(resource_id, field="资源 ID")
        except ValueError:
            return None

    def _validate_document(self, document: DocumentT) -> DocumentT:
        try:
            if isinstance(document, BaseModel):
                payload: Any = document.model_dump(mode="json")
            else:
                payload = document
            validated = self.document_type.model_validate(payload)
        except (TypeError, ValidationError, ValueError) as exc:
            raise DocumentPayloadValidationError("内容数据格式无效") from exc
        if not validated.title.strip() or len(validated.title) > 240:
            raise DocumentPayloadValidationError("内容标题必须为 1 到 240 个字符")
        if not str(validated.id).strip():
            raise DocumentPayloadValidationError("内容标识无效")
        return validated

    def _stored_document(
        self,
        record: ProjectRecord | SeriesRecord,
    ) -> VersionedDocument[DocumentT]:
        try:
            document = self.document_type.model_validate(record.payload)
        except (TypeError, ValidationError, ValueError) as exc:
            raise DocumentPayloadValidationError("数据库中的内容数据格式无效") from exc

        if document.id != str(record.id) or document.title != record.title:
            raise DocumentPayloadValidationError("数据库中的内容摘要与正文不一致")
        if isinstance(record, ProjectRecord):
            document_series_id = cast(Script, document).series_id
            record_series_id = str(record.series_id) if record.series_id else None
            if document_series_id != record_series_id:
                raise DocumentPayloadValidationError("数据库中的项目系列关联不一致")

        return VersionedDocument(
            document=document,
            version=record.version,
            schema_version=record.schema_version,
        )

    def _scope_conditions(
        self,
        user_id: int,
        workspace_id: int,
    ) -> tuple[Any, Any]:
        return (
            self.record_type.user_id == user_id,
            self.record_type.workspace_id == workspace_id,
        )

    @contextmanager
    def _session(
        self,
        context: WorkspaceContext,
        session: Session | None,
    ) -> Iterator[Session]:
        if session is not None:
            yield session
            return
        with self.database.transaction(context.identity) as transaction_session:
            yield transaction_session

    @staticmethod
    def _require_active_workspace(
        session: Session,
        user_id: int,
        workspace_id: int,
    ) -> None:
        owned_workspace_id = session.scalar(
            select(WorkspaceRecord.id).where(
                WorkspaceRecord.id == workspace_id,
                WorkspaceRecord.user_id == user_id,
                WorkspaceRecord.deleted_at.is_(None),
            )
        )
        if owned_workspace_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")

    @staticmethod
    def _project_series_id(document: BaseModel) -> int | None:
        if not isinstance(document, Script) or document.series_id is None:
            return None
        try:
            return parse_database_id(document.series_id, field="系列 ID")
        except ValueError as exc:
            raise DocumentPayloadValidationError("项目关联的系列标识无效") from exc

    @staticmethod
    def _require_owned_series(
        session: Session,
        user_id: int,
        workspace_id: int,
        series_id: int | None,
    ) -> None:
        if series_id is None:
            return
        owned_series_id = session.scalar(
            select(SeriesRecord.id).where(
                SeriesRecord.id == series_id,
                SeriesRecord.user_id == user_id,
                SeriesRecord.workspace_id == workspace_id,
                SeriesRecord.deleted_at.is_(None),
            )
        )
        if owned_series_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")

    def list(
        self,
        context: WorkspaceContext,
        *,
        session: Session | None = None,
    ) -> list[VersionedDocument[DocumentT]]:
        user_id, workspace_id = self._context_ids(context)
        statement = (
            select(self.record_type)
            .where(
                *self._scope_conditions(user_id, workspace_id),
                self.record_type.deleted_at.is_(None),
            )
            .order_by(self.record_type.created_at.asc(), self.record_type.id.asc())
        )
        with self._session(context, session) as active_session:
            records = list(active_session.scalars(statement))
            return [self._stored_document(record) for record in records]

    def get(
        self,
        context: WorkspaceContext,
        resource_id: str,
        *,
        session: Session | None = None,
    ) -> VersionedDocument[DocumentT] | None:
        user_id, workspace_id = self._context_ids(context)
        record_id = self._resource_id(resource_id)
        if record_id is None:
            return None
        statement = select(self.record_type).where(
            *self._scope_conditions(user_id, workspace_id),
            self.record_type.id == record_id,
            self.record_type.deleted_at.is_(None),
        )
        with self._session(context, session) as active_session:
            record = active_session.scalar(statement)
            return self._stored_document(record) if record is not None else None

    def require(
        self,
        context: WorkspaceContext,
        resource_id: str,
        *,
        session: Session | None = None,
    ) -> VersionedDocument[DocumentT]:
        stored = self.get(context, resource_id, session=session)
        if stored is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return stored

    def add(
        self,
        context: WorkspaceContext,
        document: DocumentT,
        *,
        session: Session | None = None,
    ) -> VersionedDocument[DocumentT]:
        user_id, workspace_id = self._context_ids(context)
        validated = self._validate_document(document)
        series_id = self._project_series_id(validated)
        record = self.record_type(
            user_id=user_id,
            workspace_id=workspace_id,
            title=validated.title,
            payload=validated.model_dump(mode="json"),
            schema_version=self.SCHEMA_VERSION,
            version=1,
        )
        if isinstance(record, ProjectRecord):
            record.series_id = series_id

        with self._session(context, session) as active_session:
            self._require_active_workspace(active_session, user_id, workspace_id)
            self._require_owned_series(
                active_session,
                user_id,
                workspace_id,
                series_id,
            )
            active_session.add(record)
            active_session.flush()
            validated = validated.model_copy(update={"id": str(record.id)})
            record.payload = validated.model_dump(mode="json")
            active_session.flush()
            return self._stored_document(record)

    def update(
        self,
        context: WorkspaceContext,
        resource_id: str,
        document: DocumentT,
        expected_version: int,
        *,
        session: Session | None = None,
    ) -> VersionedDocument[DocumentT]:
        user_id, workspace_id = self._context_ids(context)
        record_id = self._resource_id(resource_id)
        if record_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        if expected_version < 1:
            raise ValueError("内容版本必须为正整数")

        validated = self._validate_document(document)
        if validated.id != resource_id:
            raise DocumentPayloadValidationError("内容标识与请求资源不一致")
        series_id = self._project_series_id(validated)
        values: dict[str, Any] = {
            "title": validated.title,
            "payload": validated.model_dump(mode="json"),
            "schema_version": self.SCHEMA_VERSION,
            "version": self.record_type.version + 1,
            "updated_at": datetime.now(UTC),
        }
        if self.record_type is ProjectRecord:
            values["series_id"] = series_id

        statement = (
            update(self.record_type)
            .where(
                *self._scope_conditions(user_id, workspace_id),
                self.record_type.id == record_id,
                self.record_type.deleted_at.is_(None),
                self.record_type.version == expected_version,
            )
            .values(**values)
            .returning(self.record_type)
        )
        with self._session(context, session) as active_session:
            self._require_owned_series(
                active_session,
                user_id,
                workspace_id,
                series_id,
            )
            record = active_session.scalar(statement)
            if record is not None:
                return self._stored_document(record)

            current_version = active_session.scalar(
                select(self.record_type.version).where(
                    *self._scope_conditions(user_id, workspace_id),
                    self.record_type.id == record_id,
                    self.record_type.deleted_at.is_(None),
                )
            )
            if current_version is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            raise OptimisticVersionConflictError(
                "内容已在其他位置更新，请刷新后重试"
            )

    def soft_delete(
        self,
        context: WorkspaceContext,
        resource_id: str,
        *,
        expected_version: int | None = None,
        session: Session | None = None,
    ) -> None:
        user_id, workspace_id = self._context_ids(context)
        record_id = self._resource_id(resource_id)
        if record_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        deleted_at = datetime.now(UTC)
        conditions = [
            *self._scope_conditions(user_id, workspace_id),
            self.record_type.id == record_id,
            self.record_type.deleted_at.is_(None),
        ]
        if expected_version is not None:
            if expected_version < 1:
                raise ValueError("内容版本必须为正整数")
            conditions.append(self.record_type.version == expected_version)
        statement = (
            update(self.record_type)
            .where(*conditions)
            .values(
                deleted_at=deleted_at,
                retention_expires_at=deleted_at + timedelta(days=self.RETENTION_DAYS),
                version=self.record_type.version + 1,
                updated_at=deleted_at,
            )
        )
        with self._session(context, session) as active_session:
            result = active_session.execute(statement)
            if result.rowcount == 1:
                return
            if expected_version is not None:
                current_version = active_session.scalar(
                    select(self.record_type.version).where(
                        *self._scope_conditions(user_id, workspace_id),
                        self.record_type.id == record_id,
                        self.record_type.deleted_at.is_(None),
                    )
                )
                if current_version is not None:
                    raise OptimisticVersionConflictError(
                        "内容已在其他位置更新，请刷新后重试"
                    )
            raise ScopedDocumentNotFoundError("资源不存在")


class PostgresProjectRepository(PostgresDocumentRepository[Script]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, record_type=ProjectRecord, document_type=Script)


class PostgresSeriesRepository(PostgresDocumentRepository[Series]):
    def __init__(self, database: Database) -> None:
        super().__init__(database, record_type=SeriesRecord, document_type=Series)
