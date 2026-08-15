from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.apps.comic_gen.models import Character, CustomVoice, Prop, Scene

from .content_repositories import (
    DocumentPayloadValidationError,
    InvalidRepositoryContextError,
    OptimisticVersionConflictError,
    ScopedDocumentNotFoundError,
)
from .contracts import UserContext, WorkspaceContext
from .database import Database
from .db_models import (
    AssetRecord,
    ProjectRecord,
    SeriesRecord,
    WorkspaceRecord,
)
from .identifiers import parse_database_id, parse_optional_database_id


class AssetScope(str, Enum):
    PROJECT = "project"
    SERIES = "series"
    WORKSPACE = "workspace"
    SYSTEM = "system"


ASSET_DOCUMENT_TYPES: dict[str, type[BaseModel]] = {
    "character": Character,
    "scene": Scene,
    "prop": Prop,
    "voice": CustomVoice,
}


class AssetConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredAsset:
    record_id: str
    asset_type: str
    scope: AssetScope
    name: str
    document: BaseModel | dict[str, Any]
    provenance: Mapping[str, Any]
    media_object_id: str | None
    version: int
    schema_version: int

    @property
    def domain_id(self) -> str:
        if isinstance(self.document, BaseModel):
            return str(self.document.id)
        return str(self.document.get("id", ""))


def _context_ids(context: WorkspaceContext) -> tuple[int, int]:
    if not isinstance(context, WorkspaceContext) or not isinstance(
        context.identity, UserContext
    ):
        raise InvalidRepositoryContextError(
            "资产仓储必须包含用户和工作区上下文"
        )
    try:
        return (
            parse_database_id(context.identity.user_id, field="用户 ID"),
            parse_database_id(context.workspace_id, field="工作区 ID"),
        )
    except ValueError as exc:
        raise InvalidRepositoryContextError("用户或工作区标识无效") from exc


def _resource_id(value: str | int | None) -> int | None:
    try:
        return parse_optional_database_id(value, field="资源 ID")
    except ValueError:
        return None


def _validate_asset_type(asset_type: str) -> str:
    if asset_type not in {*ASSET_DOCUMENT_TYPES, "other"}:
        raise DocumentPayloadValidationError("资产类型无效")
    return asset_type


def _validate_payload(
    asset_type: str,
    document: BaseModel | Mapping[str, Any],
) -> BaseModel | dict[str, Any]:
    canonical_type = _validate_asset_type(asset_type)
    payload: Any = (
        document.model_dump(mode="json")
        if isinstance(document, BaseModel)
        else dict(document)
    )
    if canonical_type == "other":
        if not isinstance(payload, dict) or not str(payload.get("id", "")).strip():
            raise DocumentPayloadValidationError("其他资产必须包含有效 id")
        return payload
    try:
        return ASSET_DOCUMENT_TYPES[canonical_type].model_validate(payload)
    except (TypeError, ValidationError, ValueError) as exc:
        raise DocumentPayloadValidationError("资产数据格式无效") from exc


def _payload_name(document: BaseModel | dict[str, Any]) -> str:
    if isinstance(document, CustomVoice):
        name = document.label
    elif isinstance(document, BaseModel):
        name = str(getattr(document, "name", ""))
    else:
        name = str(document.get("name", ""))
    name = name.strip()
    if not name or len(name) > 240:
        raise DocumentPayloadValidationError("资产名称必须为 1 到 240 个字符")
    return name


def _payload_json(document: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(document, BaseModel):
        return document.model_dump(mode="json")
    return dict(document)


def _stored_asset(record: AssetRecord) -> StoredAsset:
    document = _validate_payload(record.asset_type, record.payload)
    name = _payload_name(document)
    if name != record.name:
        raise DocumentPayloadValidationError("数据库中的资产摘要与正文不一致")
    return StoredAsset(
        record_id=str(record.id),
        asset_type=record.asset_type,
        scope=AssetScope(record.scope),
        name=record.name,
        document=document,
        provenance=dict(record.provenance),
        media_object_id=(str(record.media_object_id) if record.media_object_id else None),
        version=record.version,
        schema_version=record.schema_version,
    )


class _OwnedAssetRepository:
    SCHEMA_VERSION = 1
    RETENTION_DAYS = 30

    def __init__(self, database: Database, scope: AssetScope) -> None:
        if scope is AssetScope.SYSTEM:
            raise ValueError("系统资产必须使用只读仓储")
        self.database = database
        self.scope = scope

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

    def _parent_id(self, parent_id: str | None) -> int | None:
        if self.scope is AssetScope.WORKSPACE:
            if parent_id is not None:
                raise ValueError("工作区资产不接受项目或系列标识")
            return None
        parsed = _resource_id(parent_id)
        if parsed is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return parsed

    def _conditions(
        self,
        user_id: int,
        workspace_id: int,
        parent_id: int | None,
    ) -> list[Any]:
        conditions = [
            AssetRecord.user_id == user_id,
            AssetRecord.workspace_id == workspace_id,
            AssetRecord.scope == self.scope.value,
        ]
        if self.scope is AssetScope.PROJECT:
            conditions.extend(
                [
                    AssetRecord.project_id == parent_id,
                    AssetRecord.series_id.is_(None),
                ]
            )
        elif self.scope is AssetScope.SERIES:
            conditions.extend(
                [
                    AssetRecord.series_id == parent_id,
                    AssetRecord.project_id.is_(None),
                ]
            )
        else:
            conditions.extend(
                [
                    AssetRecord.project_id.is_(None),
                    AssetRecord.series_id.is_(None),
                ]
            )
        return conditions

    def _require_parent(
        self,
        session: Session,
        user_id: int,
        workspace_id: int,
        parent_id: int | None,
    ) -> None:
        if self.scope is AssetScope.PROJECT:
            existing_id = session.scalar(
                select(ProjectRecord.id).where(
                    ProjectRecord.id == parent_id,
                    ProjectRecord.user_id == user_id,
                    ProjectRecord.workspace_id == workspace_id,
                    ProjectRecord.deleted_at.is_(None),
                )
            )
        elif self.scope is AssetScope.SERIES:
            existing_id = session.scalar(
                select(SeriesRecord.id).where(
                    SeriesRecord.id == parent_id,
                    SeriesRecord.user_id == user_id,
                    SeriesRecord.workspace_id == workspace_id,
                    SeriesRecord.deleted_at.is_(None),
                )
            )
        else:
            existing_id = session.scalar(
                select(WorkspaceRecord.id).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.user_id == user_id,
                    WorkspaceRecord.deleted_at.is_(None),
                )
            )
        if existing_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")

    def list(
        self,
        context: WorkspaceContext,
        parent_id: str | None = None,
        *,
        asset_type: str | None = None,
        session: Session | None = None,
    ) -> list[StoredAsset]:
        user_id, workspace_id = _context_ids(context)
        canonical_parent_id = self._parent_id(parent_id)
        if asset_type is not None:
            _validate_asset_type(asset_type)
        conditions = [
            *self._conditions(user_id, workspace_id, canonical_parent_id),
            AssetRecord.deleted_at.is_(None),
        ]
        if asset_type is not None:
            conditions.append(AssetRecord.asset_type == asset_type)
        statement = (
            select(AssetRecord)
            .where(*conditions)
            .order_by(AssetRecord.created_at.asc(), AssetRecord.id.asc())
        )
        with self._session(context, session) as active_session:
            self._require_parent(
                active_session,
                user_id,
                workspace_id,
                canonical_parent_id,
            )
            return [
                _stored_asset(record)
                for record in active_session.scalars(statement)
            ]

    def get(
        self,
        context: WorkspaceContext,
        parent_id: str | None,
        record_id: str,
        *,
        session: Session | None = None,
    ) -> StoredAsset | None:
        user_id, workspace_id = _context_ids(context)
        canonical_parent_id = self._parent_id(parent_id)
        canonical_record_id = _resource_id(record_id)
        if canonical_record_id is None:
            return None
        statement = select(AssetRecord).where(
            *self._conditions(user_id, workspace_id, canonical_parent_id),
            AssetRecord.id == canonical_record_id,
            AssetRecord.deleted_at.is_(None),
        )
        with self._session(context, session) as active_session:
            self._require_parent(
                active_session,
                user_id,
                workspace_id,
                canonical_parent_id,
            )
            record = active_session.scalar(statement)
            return _stored_asset(record) if record is not None else None

    def require(
        self,
        context: WorkspaceContext,
        parent_id: str | None,
        record_id: str,
        *,
        session: Session | None = None,
    ) -> StoredAsset:
        stored = self.get(
            context,
            parent_id,
            record_id,
            session=session,
        )
        if stored is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return stored

    def find_by_domain_id(
        self,
        context: WorkspaceContext,
        parent_id: str | None,
        asset_type: str,
        domain_id: str,
        *,
        session: Session | None = None,
    ) -> StoredAsset | None:
        for asset in self.list(
            context,
            parent_id,
            asset_type=asset_type,
            session=session,
        ):
            if asset.domain_id == domain_id:
                return asset
        return None

    def _provenance(
        self,
        context: WorkspaceContext,
        parent_id: int | None,
        provenance: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        result = dict(provenance or {})
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            raise DocumentPayloadValidationError("资产来源信息必须是 JSON 数据") from exc
        result["created_by_user_id"] = context.identity.user_id
        result["workspace_id"] = context.workspace_id
        result["scope"] = self.scope.value
        result.setdefault("origin", "created")
        if self.scope is AssetScope.PROJECT:
            result["project_id"] = str(parent_id)
            result.pop("series_id", None)
        elif self.scope is AssetScope.SERIES:
            result["series_id"] = str(parent_id)
            result.pop("project_id", None)
        else:
            result.pop("project_id", None)
            result.pop("series_id", None)
        return result

    def add(
        self,
        context: WorkspaceContext,
        parent_id: str | None,
        asset_type: str,
        document: BaseModel | Mapping[str, Any],
        *,
        provenance: Mapping[str, Any] | None = None,
        media_object_id: str | None = None,
        session: Session | None = None,
    ) -> StoredAsset:
        user_id, workspace_id = _context_ids(context)
        canonical_parent_id = self._parent_id(parent_id)
        validated = _validate_payload(asset_type, document)
        canonical_media_id = _resource_id(media_object_id)
        if media_object_id is not None and canonical_media_id is None:
            raise DocumentPayloadValidationError("媒体标识无效")

        with self._session(context, session) as active_session:
            self._require_parent(
                active_session,
                user_id,
                workspace_id,
                canonical_parent_id,
            )
            existing = self.find_by_domain_id(
                context,
                parent_id,
                asset_type,
                str(getattr(validated, "id", _payload_json(validated).get("id"))),
                session=active_session,
            )
            if existing is not None:
                raise AssetConflictError("同一作用域中已存在该资产标识")
            record = AssetRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=(
                    canonical_parent_id
                    if self.scope is AssetScope.PROJECT
                    else None
                ),
                series_id=(
                    canonical_parent_id
                    if self.scope is AssetScope.SERIES
                    else None
                ),
                media_object_id=canonical_media_id,
                scope=self.scope.value,
                asset_type=asset_type,
                name=_payload_name(validated),
                payload=_payload_json(validated),
                provenance=self._provenance(
                    context,
                    canonical_parent_id,
                    provenance,
                ),
                schema_version=self.SCHEMA_VERSION,
                version=1,
            )
            active_session.add(record)
            active_session.flush()
            return _stored_asset(record)

    def update(
        self,
        context: WorkspaceContext,
        parent_id: str | None,
        record_id: str,
        document: BaseModel | Mapping[str, Any],
        expected_version: int,
        *,
        media_object_id: str | None = None,
        session: Session | None = None,
    ) -> StoredAsset:
        user_id, workspace_id = _context_ids(context)
        canonical_parent_id = self._parent_id(parent_id)
        canonical_record_id = _resource_id(record_id)
        if canonical_record_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        if expected_version < 1:
            raise ValueError("资产版本必须为正整数")
        current = self.require(
            context,
            parent_id,
            record_id,
            session=session,
        )
        validated = _validate_payload(current.asset_type, document)
        if current.domain_id != str(
            getattr(validated, "id", _payload_json(validated).get("id"))
        ):
            raise DocumentPayloadValidationError("资产标识不可修改")
        canonical_media_id = _resource_id(media_object_id)
        if media_object_id is not None and canonical_media_id is None:
            raise DocumentPayloadValidationError("媒体标识无效")
        if media_object_id is None:
            canonical_media_id = _resource_id(current.media_object_id)

        statement = (
            update(AssetRecord)
            .where(
                *self._conditions(user_id, workspace_id, canonical_parent_id),
                AssetRecord.id == canonical_record_id,
                AssetRecord.deleted_at.is_(None),
                AssetRecord.version == expected_version,
            )
            .values(
                name=_payload_name(validated),
                payload=_payload_json(validated),
                media_object_id=canonical_media_id,
                schema_version=self.SCHEMA_VERSION,
                version=AssetRecord.version + 1,
                updated_at=datetime.now(UTC),
            )
            .returning(AssetRecord)
        )
        with self._session(context, session) as active_session:
            record = active_session.scalar(statement)
            if record is not None:
                return _stored_asset(record)
            current_version = active_session.scalar(
                select(AssetRecord.version).where(
                    *self._conditions(user_id, workspace_id, canonical_parent_id),
                    AssetRecord.id == canonical_record_id,
                    AssetRecord.deleted_at.is_(None),
                )
            )
            if current_version is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            raise OptimisticVersionConflictError(
                "资产已在其他位置更新，请刷新后重试"
            )

    def soft_delete(
        self,
        context: WorkspaceContext,
        parent_id: str | None,
        record_id: str,
        expected_version: int,
        *,
        session: Session | None = None,
    ) -> None:
        user_id, workspace_id = _context_ids(context)
        canonical_parent_id = self._parent_id(parent_id)
        canonical_record_id = _resource_id(record_id)
        if canonical_record_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        deleted_at = datetime.now(UTC)
        statement = (
            update(AssetRecord)
            .where(
                *self._conditions(user_id, workspace_id, canonical_parent_id),
                AssetRecord.id == canonical_record_id,
                AssetRecord.deleted_at.is_(None),
                AssetRecord.version == expected_version,
            )
            .values(
                deleted_at=deleted_at,
                retention_expires_at=deleted_at + timedelta(days=self.RETENTION_DAYS),
                version=AssetRecord.version + 1,
                updated_at=deleted_at,
            )
        )
        with self._session(context, session) as active_session:
            result = active_session.execute(statement)
            if result.rowcount == 1:
                return
            current_version = active_session.scalar(
                select(AssetRecord.version).where(
                    *self._conditions(user_id, workspace_id, canonical_parent_id),
                    AssetRecord.id == canonical_record_id,
                    AssetRecord.deleted_at.is_(None),
                )
            )
            if current_version is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            raise OptimisticVersionConflictError(
                "资产已在其他位置更新，请刷新后重试"
            )


class PostgresProjectAssetRepository(_OwnedAssetRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database, AssetScope.PROJECT)


class PostgresSeriesAssetRepository(_OwnedAssetRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database, AssetScope.SERIES)


class PostgresWorkspaceAssetRepository(_OwnedAssetRepository):
    def __init__(self, database: Database) -> None:
        super().__init__(database, AssetScope.WORKSPACE)


class ReadOnlySystemAssetRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list(
        self,
        context: WorkspaceContext,
        *,
        asset_type: str | None = None,
        session: Session | None = None,
    ) -> list[StoredAsset]:
        _context_ids(context)
        if asset_type is not None:
            _validate_asset_type(asset_type)
        conditions = [
            AssetRecord.scope == AssetScope.SYSTEM.value,
            AssetRecord.user_id.is_(None),
            AssetRecord.workspace_id.is_(None),
            AssetRecord.deleted_at.is_(None),
        ]
        if asset_type is not None:
            conditions.append(AssetRecord.asset_type == asset_type)
        statement = (
            select(AssetRecord)
            .where(*conditions)
            .order_by(AssetRecord.created_at.asc(), AssetRecord.id.asc())
        )
        if session is not None:
            return [_stored_asset(record) for record in session.scalars(statement)]
        with self.database.transaction(context.identity) as active_session:
            return [
                _stored_asset(record)
                for record in active_session.scalars(statement)
            ]

    def get(
        self,
        context: WorkspaceContext,
        record_id: str,
        *,
        session: Session | None = None,
    ) -> StoredAsset | None:
        canonical_record_id = _resource_id(record_id)
        if canonical_record_id is None:
            return None
        for asset in self.list(context, session=session):
            if asset.record_id == str(canonical_record_id):
                return asset
        return None

    def find_by_domain_id(
        self,
        context: WorkspaceContext,
        asset_type: str,
        domain_id: str,
        *,
        session: Session | None = None,
    ) -> StoredAsset | None:
        for asset in self.list(
            context,
            asset_type=asset_type,
            session=session,
        ):
            if asset.domain_id == domain_id:
                return asset
        return None


class AssetResolver:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.project_assets = PostgresProjectAssetRepository(database)
        self.series_assets = PostgresSeriesAssetRepository(database)
        self.workspace_assets = PostgresWorkspaceAssetRepository(database)
        self.system_assets = ReadOnlySystemAssetRepository(database)

    def resolve(
        self,
        context: WorkspaceContext,
        asset_type: str,
        domain_id: str,
        *,
        project_id: str | None = None,
        series_id: str | None = None,
    ) -> StoredAsset | None:
        user_id, workspace_id = _context_ids(context)
        canonical_project_id = _resource_id(project_id)
        canonical_series_id = _resource_id(series_id)
        if project_id is not None and canonical_project_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        if series_id is not None and canonical_series_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")

        with self.database.transaction(context.identity) as session:
            if canonical_project_id is not None:
                project_series_id = session.scalar(
                    select(ProjectRecord.series_id).where(
                        ProjectRecord.id == canonical_project_id,
                        ProjectRecord.user_id == user_id,
                        ProjectRecord.workspace_id == workspace_id,
                        ProjectRecord.deleted_at.is_(None),
                    )
                )
                project_exists = session.scalar(
                    select(ProjectRecord.id).where(
                        ProjectRecord.id == canonical_project_id,
                        ProjectRecord.user_id == user_id,
                        ProjectRecord.workspace_id == workspace_id,
                        ProjectRecord.deleted_at.is_(None),
                    )
                )
                if project_exists is None:
                    raise ScopedDocumentNotFoundError("资源不存在")
                if (
                    canonical_series_id is not None
                    and canonical_series_id != project_series_id
                ):
                    raise ScopedDocumentNotFoundError("资源不存在")
                canonical_series_id = project_series_id
                project_asset = self.project_assets.find_by_domain_id(
                    context,
                    str(canonical_project_id),
                    asset_type,
                    domain_id,
                    session=session,
                )
                if project_asset is not None:
                    return project_asset

            if canonical_series_id is not None:
                series_asset = self.series_assets.find_by_domain_id(
                    context,
                    str(canonical_series_id),
                    asset_type,
                    domain_id,
                    session=session,
                )
                if series_asset is not None:
                    return series_asset

            workspace_asset = self.workspace_assets.find_by_domain_id(
                context,
                None,
                asset_type,
                domain_id,
                session=session,
            )
            if workspace_asset is not None:
                return workspace_asset
            return self.system_assets.find_by_domain_id(
                context,
                asset_type,
                domain_id,
                session=session,
            )
