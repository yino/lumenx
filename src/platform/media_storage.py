from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any, Protocol

import oss2
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .content_repositories import (
    InvalidRepositoryContextError,
    ScopedDocumentNotFoundError,
)
from .contracts import MediaWrite, StoredMedia, UserContext, WorkspaceContext
from .database import Database
from .db_models import MediaObjectRecord, ProjectRecord, WorkspaceRecord
from .identifiers import parse_database_id, parse_optional_database_id
from .settings import DeploymentSettings


class MediaValidationError(ValueError):
    pass


class MediaStorageError(RuntimeError):
    pass


class MediaLifecycleConflictError(RuntimeError):
    pass


class PrivateObjectStore(Protocol):
    def put(self, object_key: str, content: bytes, content_type: str) -> None: ...

    def signed_get_url(self, object_key: str, expires_seconds: int) -> str: ...

    def delete(self, object_key: str) -> None: ...


class OSSPrivateObjectStore:
    def __init__(self, settings: DeploymentSettings) -> None:
        if (
            not settings.oss_endpoint
            or not settings.oss_bucket_name
            or not settings.oss_access_key_id
            or not settings.oss_access_key_secret
        ):
            raise MediaStorageError("私有对象存储配置不完整")
        if not settings.oss_private:
            raise MediaStorageError("云端对象存储必须为私有")
        auth = oss2.Auth(
            settings.oss_access_key_id,
            settings.oss_access_key_secret.get_secret_value(),
        )
        self.bucket = oss2.Bucket(
            auth,
            settings.oss_endpoint,
            settings.oss_bucket_name,
            connect_timeout=5,
        )

    def put(self, object_key: str, content: bytes, content_type: str) -> None:
        result = self.bucket.put_object(
            object_key,
            content,
            headers={"Content-Type": content_type},
        )
        if result.status != 200:
            raise MediaStorageError("对象存储写入失败")

    def signed_get_url(self, object_key: str, expires_seconds: int) -> str:
        url = self.bucket.sign_url(
            "GET",
            object_key,
            expires_seconds,
            slash_safe=True,
        )
        if url.startswith("http://"):
            return f"https://{url[7:]}"
        return url

    def delete(self, object_key: str) -> None:
        result = self.bucket.delete_object(object_key)
        if result.status not in {200, 204}:
            raise MediaStorageError("对象存储删除失败")


def _context_ids(context: WorkspaceContext) -> tuple[int, int]:
    if not isinstance(context, WorkspaceContext) or not isinstance(
        context.identity,
        UserContext,
    ):
        raise InvalidRepositoryContextError(
            "媒体仓储必须包含用户和工作区上下文"
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


class PostgresMediaRepository:
    RETENTION_DAYS = 30

    def __init__(self, database: Database) -> None:
        self.database = database

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
    def _validate_provenance(
        context: WorkspaceContext,
        project_id: str | None,
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = dict(provenance)
        try:
            json.dumps(result)
        except (TypeError, ValueError) as exc:
            raise MediaValidationError("媒体来源信息必须是 JSON 数据") from exc
        result["created_by_user_id"] = context.identity.user_id
        result["workspace_id"] = context.workspace_id
        if project_id is None:
            result.pop("project_id", None)
        else:
            result["project_id"] = project_id
        result.setdefault("origin", "upload")
        return result

    @staticmethod
    def _require_scope(
        session: Session,
        user_id: int,
        workspace_id: int,
        project_id: int | None,
    ) -> None:
        workspace = session.scalar(
            select(WorkspaceRecord.id).where(
                WorkspaceRecord.id == workspace_id,
                WorkspaceRecord.user_id == user_id,
                WorkspaceRecord.deleted_at.is_(None),
            )
        )
        if workspace is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        if project_id is None:
            return
        project = session.scalar(
            select(ProjectRecord.id).where(
                ProjectRecord.id == project_id,
                ProjectRecord.user_id == user_id,
                ProjectRecord.workspace_id == workspace_id,
                ProjectRecord.deleted_at.is_(None),
            )
        )
        if project is None:
            raise ScopedDocumentNotFoundError("资源不存在")

    def create_pending(
        self,
        context: WorkspaceContext,
        *,
        project_id: str | None,
        object_key: str,
        mime_type: str,
        size_bytes: int,
        checksum_sha256: str,
        provenance: Mapping[str, Any],
        session: Session | None = None,
    ) -> MediaObjectRecord:
        user_id, workspace_id = _context_ids(context)
        canonical_project_id = _resource_id(project_id)
        if project_id is not None and canonical_project_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        record = MediaObjectRecord(
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=canonical_project_id,
            object_key=object_key,
            mime_type=mime_type,
            size_bytes=size_bytes,
            checksum_sha256=checksum_sha256,
            lifecycle_state="pending",
            provenance=self._validate_provenance(
                context,
                project_id,
                provenance,
            ),
        )
        with self._session(context, session) as active_session:
            self._require_scope(
                active_session,
                user_id,
                workspace_id,
                canonical_project_id,
            )
            active_session.add(record)
            active_session.flush()
            return record

    def get(
        self,
        context: WorkspaceContext,
        media_id: str,
        *,
        active_only: bool = True,
        session: Session | None = None,
    ) -> MediaObjectRecord | None:
        user_id, workspace_id = _context_ids(context)
        canonical_media_id = _resource_id(media_id)
        if canonical_media_id is None:
            return None
        conditions = [
            MediaObjectRecord.id == canonical_media_id,
            MediaObjectRecord.user_id == user_id,
            MediaObjectRecord.workspace_id == workspace_id,
        ]
        if active_only:
            conditions.extend(
                [
                    MediaObjectRecord.lifecycle_state == "active",
                    MediaObjectRecord.deleted_at.is_(None),
                ]
            )
        with self._session(context, session) as active_session:
            return active_session.scalar(select(MediaObjectRecord).where(*conditions))

    def get_by_object_key(
        self,
        context: WorkspaceContext,
        object_key: str,
        *,
        active_only: bool = True,
    ) -> MediaObjectRecord | None:
        user_id, workspace_id = _context_ids(context)
        conditions = [
            MediaObjectRecord.object_key == object_key,
            MediaObjectRecord.user_id == user_id,
            MediaObjectRecord.workspace_id == workspace_id,
        ]
        if active_only:
            conditions.extend(
                [
                    MediaObjectRecord.lifecycle_state == "active",
                    MediaObjectRecord.deleted_at.is_(None),
                ]
            )
        with self.database.transaction(context.identity) as session:
            return session.scalar(select(MediaObjectRecord).where(*conditions))

    def require(
        self,
        context: WorkspaceContext,
        media_id: str,
        *,
        active_only: bool = True,
        session: Session | None = None,
    ) -> MediaObjectRecord:
        record = self.get(
            context,
            media_id,
            active_only=active_only,
            session=session,
        )
        if record is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return record

    def set_lifecycle(
        self,
        context: WorkspaceContext,
        media_id: str,
        *,
        from_state: str,
        to_state: str,
        session: Session | None = None,
    ) -> MediaObjectRecord:
        user_id, workspace_id = _context_ids(context)
        canonical_media_id = _resource_id(media_id)
        if canonical_media_id is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        values: dict[str, Any] = {"lifecycle_state": to_state}
        if to_state == "deleted":
            deleted_at = datetime.now(UTC)
            values.update(
                {
                    "deleted_at": deleted_at,
                    "retention_expires_at": deleted_at
                    + timedelta(days=self.RETENTION_DAYS),
                }
            )
        statement = (
            update(MediaObjectRecord)
            .where(
                MediaObjectRecord.id == canonical_media_id,
                MediaObjectRecord.user_id == user_id,
                MediaObjectRecord.workspace_id == workspace_id,
                MediaObjectRecord.lifecycle_state == from_state,
            )
            .values(**values)
            .returning(MediaObjectRecord)
        )
        with self._session(context, session) as active_session:
            record = active_session.scalar(statement)
            if record is not None:
                return record
            existing = active_session.scalar(
                select(MediaObjectRecord.lifecycle_state).where(
                    MediaObjectRecord.id == canonical_media_id,
                    MediaObjectRecord.user_id == user_id,
                    MediaObjectRecord.workspace_id == workspace_id,
                )
            )
            if existing is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            raise MediaLifecycleConflictError("媒体状态已变化，请刷新后重试")


class CloudMediaStorage:
    MAX_SIGNED_URL_SECONDS = 15 * 60

    def __init__(
        self,
        database: Database,
        object_store: PrivateObjectStore,
        *,
        namespace_prefix: str = "",
    ) -> None:
        self.database = database
        self.object_store = object_store
        self.repository = PostgresMediaRepository(database)
        self.namespace_prefix = namespace_prefix.strip("/")

    @staticmethod
    def _validate_media(media: MediaWrite) -> None:
        if not isinstance(media.content, bytes) or not media.content:
            raise MediaValidationError("媒体内容不能为空")
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media.content_type):
            raise MediaValidationError("媒体 MIME 类型无效")
        if len(media.content_type) > 255:
            raise MediaValidationError("媒体 MIME 类型过长")
        if not PurePath(media.filename).name:
            raise MediaValidationError("媒体文件名无效")

    def _object_key(
        self,
        context: WorkspaceContext,
        storage_identifier: str,
        filename: str,
        project_id: str | None,
    ) -> str:
        suffix = PurePath(PurePath(filename).name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            suffix = ""
        project_segment = project_id or "shared"
        parts = []
        if self.namespace_prefix:
            parts.append(self.namespace_prefix)
        parts.extend(
            [
                "users",
                context.identity.user_id,
                "workspaces",
                context.workspace_id,
                "projects",
                project_segment,
                f"{storage_identifier}{suffix}",
            ]
        )
        return "/".join(parts)

    @staticmethod
    def _stored(record: MediaObjectRecord) -> StoredMedia:
        return StoredMedia(
            media_id=str(record.id),
            object_key=record.object_key,
            content_type=record.mime_type,
            size_bytes=record.size_bytes,
            checksum_sha256=record.checksum_sha256,
        )

    def _store_with_key(
        self,
        context: WorkspaceContext,
        media: MediaWrite,
        storage_identifier: str,
    ) -> StoredMedia:
        self._validate_media(media)
        object_key = self._object_key(
            context,
            storage_identifier,
            media.filename,
            media.project_id,
        )
        checksum = hashlib.sha256(media.content).hexdigest()
        pending = self.repository.create_pending(
            context,
            project_id=media.project_id,
            object_key=object_key,
            mime_type=media.content_type,
            size_bytes=len(media.content),
            checksum_sha256=checksum,
            provenance={**media.provenance, "filename": PurePath(media.filename).name},
        )
        try:
            self.object_store.put(object_key, media.content, media.content_type)
        except Exception as exc:
            self.repository.set_lifecycle(
                context,
                str(pending.id),
                from_state="pending",
                to_state="failed",
            )
            raise MediaStorageError("媒体上传失败") from exc

        try:
            active = self.repository.set_lifecycle(
                context,
                str(pending.id),
                from_state="pending",
                to_state="active",
            )
        except Exception:
            try:
                self.object_store.delete(object_key)
            except Exception:
                pass
            raise
        return self._stored(active or pending)

    def store(self, context: WorkspaceContext, media: MediaWrite) -> StoredMedia:
        return self._store_with_key(
            context,
            media,
            f"upload-{secrets.token_hex(16)}",
        )

    def store_generated(
        self,
        context: WorkspaceContext,
        media: MediaWrite,
        *,
        task_id: str,
        output_index: int,
    ) -> StoredMedia:
        try:
            canonical_task_id = parse_database_id(task_id, field="AI 任务 ID")
        except ValueError as exc:
            raise MediaValidationError("AI 任务标识无效") from exc
        if not isinstance(output_index, int) or isinstance(output_index, bool) or output_index < 0:
            raise MediaValidationError("AI 输出序号无效")
        self._validate_media(media)
        storage_identifier = f"task-{canonical_task_id}-output-{output_index}"
        object_key = self._object_key(
            context,
            storage_identifier,
            media.filename,
            media.project_id,
        )
        checksum = hashlib.sha256(media.content).hexdigest()
        existing = self.repository.get_by_object_key(
            context,
            object_key,
            active_only=False,
        )
        if existing is not None:
            if (
                existing.lifecycle_state == "active"
                and existing.checksum_sha256 == checksum
                and existing.mime_type == media.content_type
                and existing.size_bytes == len(media.content)
            ):
                return self._stored(existing)
            raise MediaLifecycleConflictError("AI 输出媒体已存在但内容不一致")
        return self._store_with_key(context, media, storage_identifier)

    def store_imported(
        self,
        context: WorkspaceContext,
        media: MediaWrite,
        *,
        source_fingerprint: str,
        source_key: str,
    ) -> StoredMedia:
        if not re.fullmatch(r"[a-f0-9]{64}", source_fingerprint):
            raise MediaValidationError("导入源指纹无效")
        normalized_source_key = source_key.strip().replace("\\", "/")
        if not normalized_source_key or normalized_source_key.startswith("/"):
            raise MediaValidationError("导入媒体相对路径无效")
        self._validate_media(media)
        import_key = hashlib.sha256(
            f"{source_fingerprint}:{normalized_source_key}".encode("utf-8")
        ).hexdigest()
        storage_identifier = f"import-{import_key}"
        object_key = self._object_key(
            context,
            storage_identifier,
            media.filename,
            media.project_id,
        )
        checksum = hashlib.sha256(media.content).hexdigest()
        existing = self.repository.get_by_object_key(
            context,
            object_key,
            active_only=False,
        )
        if existing is not None:
            if (
                existing.lifecycle_state == "active"
                and existing.checksum_sha256 == checksum
                and existing.mime_type == media.content_type
                and existing.size_bytes == len(media.content)
            ):
                return self._stored(existing)
            if (
                existing.lifecycle_state in {"pending", "failed"}
                and existing.checksum_sha256 == checksum
                and existing.mime_type == media.content_type
                and existing.size_bytes == len(media.content)
            ):
                if existing.lifecycle_state == "failed":
                    existing = self.repository.set_lifecycle(
                        context,
                        str(existing.id),
                        from_state="failed",
                        to_state="pending",
                    )
                try:
                    self.object_store.put(
                        existing.object_key,
                        media.content,
                        media.content_type,
                    )
                except Exception as exc:
                    self.repository.set_lifecycle(
                        context,
                        str(existing.id),
                        from_state="pending",
                        to_state="failed",
                    )
                    raise MediaStorageError("导入媒体上传失败") from exc
                active = self.repository.set_lifecycle(
                    context,
                    str(existing.id),
                    from_state="pending",
                    to_state="active",
                )
                return self._stored(active)
            raise MediaLifecycleConflictError("导入媒体已存在但内容不一致")
        imported_media = MediaWrite(
            content=media.content,
            content_type=media.content_type,
            filename=media.filename,
            project_id=media.project_id,
            provenance={
                **media.provenance,
                "origin": "local_import",
                "source_fingerprint": source_fingerprint,
                "source_key": normalized_source_key,
            },
        )
        return self._store_with_key(context, imported_media, storage_identifier)

    def authorized_url(
        self,
        context: WorkspaceContext,
        media_id: str,
        expires_at: datetime,
    ) -> str:
        if expires_at.tzinfo is None:
            raise MediaValidationError("授权链接过期时间必须包含时区")
        seconds = math.ceil((expires_at - datetime.now(UTC)).total_seconds())
        if seconds < 1 or seconds > self.MAX_SIGNED_URL_SECONDS:
            raise MediaValidationError("授权链接有效期必须在 15 分钟以内")
        record = self.repository.require(context, media_id)
        try:
            url = self.object_store.signed_get_url(record.object_key, seconds)
        except Exception as exc:
            raise MediaStorageError("媒体授权链接生成失败") from exc
        if not url:
            raise MediaStorageError("媒体授权链接生成失败")
        return url

    def delete(self, context: WorkspaceContext, media_id: str) -> None:
        self.repository.set_lifecycle(
            context,
            media_id,
            from_state="active",
            to_state="deleted",
        )
