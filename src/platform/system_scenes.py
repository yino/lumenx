from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import func, select

from .admin_access import require_platform_admin_context
from .admin_contracts import SystemScenePayload, normalize_chinese_reason
from .contracts import AdminContext, UserContext, WorkspaceContext
from .db_models import (
    AssetRecord,
    AuditEventRecord,
    MediaObjectRecord,
    ProjectRecord,
    WorkspaceRecord,
)
from .identifiers import parse_database_id
from .media_storage import CloudMediaStorage, MediaStorageError, MediaValidationError


class SystemSceneNotFoundError(LookupError):
    pass


class SystemSceneConflictError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "SYSTEM_SCENE_CONFLICT",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class SystemSceneView:
    record: AssetRecord
    payload: SystemScenePayload
    usage_count: int


@dataclass(frozen=True, slots=True)
class SystemScenePage:
    items: list[SystemSceneView]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class StoredSystemMedia:
    media_id: int
    mime_type: str
    size_bytes: int
    checksum_sha256: str


_SCENE_FIELDS = set(SystemScenePayload.model_fields)


def _validated_payload(value: SystemScenePayload | dict[str, Any]) -> SystemScenePayload:
    if isinstance(value, SystemScenePayload):
        return value
    return SystemScenePayload.model_validate(
        {key: raw for key, raw in value.items() if key in _SCENE_FIELDS}
    )


def _database_payload(payload: SystemScenePayload, *, domain_id: str) -> dict[str, Any]:
    return {"id": domain_id, **payload.model_dump(mode="json")}


def _safe_audit_payload(payload: SystemScenePayload) -> dict[str, Any]:
    return {
        "name": payload.name,
        "category": payload.category,
        "tags": payload.tags,
        "visibility": payload.visibility,
        "sort_order": payload.sort_order,
        "schema_version": payload.schema_version,
        "cover_media_id": payload.cover_media_id,
    }


class SystemSceneCatalogService:
    MAX_COVER_BYTES = 10 * 1024 * 1024
    ALLOWED_COVER_TYPES = {
        "image/png": (".png", lambda content: content.startswith(b"\x89PNG\r\n\x1a\n")),
        "image/jpeg": (".jpg", lambda content: content.startswith(b"\xff\xd8\xff")),
        "image/webp": (
            ".webp",
            lambda content: len(content) >= 12
            and content.startswith(b"RIFF")
            and content[8:12] == b"WEBP",
        ),
    }

    def __init__(self, database: Any, media_storage: CloudMediaStorage | None = None) -> None:
        self.database = database
        self.media_storage = media_storage

    @staticmethod
    def _admin_id(identity: AdminContext) -> int:
        require_platform_admin_context(identity)
        return parse_database_id(identity.admin_id, field="管理员 ID")

    @staticmethod
    def _reason(value: str) -> str:
        try:
            return normalize_chinese_reason(value)
        except ValueError as exc:
            raise SystemSceneConflictError(str(exc), code="REASON_INVALID") from exc

    @staticmethod
    def _scene_record(session: Any, scene_id: int, *, lock: bool = False) -> AssetRecord:
        statement = select(AssetRecord).where(
            AssetRecord.id == scene_id,
            AssetRecord.scope == "system",
            AssetRecord.asset_type == "scene",
        )
        if lock:
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if record is None:
            raise SystemSceneNotFoundError("系统场景不存在")
        return record

    @staticmethod
    def _usage_count(session: Any, scene_id: int) -> int:
        return int(
            session.scalar(
                select(func.count(AssetRecord.id)).where(
                    AssetRecord.scope != "system",
                    AssetRecord.deleted_at.is_(None),
                    AssetRecord.provenance["source_system_scene_id"].as_string()
                    == str(scene_id),
                )
            )
            or 0
        )

    @staticmethod
    def _require_system_media(session: Any, media_id: int | None) -> None:
        if media_id is None:
            return
        media = session.scalar(
            select(MediaObjectRecord).where(
                MediaObjectRecord.id == media_id,
                MediaObjectRecord.scope == "system",
                MediaObjectRecord.lifecycle_state == "active",
                MediaObjectRecord.deleted_at.is_(None),
            )
        )
        if media is None or not media.mime_type.startswith("image/"):
            raise SystemSceneConflictError(
                "封面媒体不存在、不可用或不是图片",
                code="SYSTEM_MEDIA_INVALID",
            )

    @staticmethod
    def _audit(
        session: Any,
        *,
        actor_id: int,
        record: AssetRecord,
        action: str,
        reason: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        correlation_id: str | None,
    ) -> None:
        session.add(
            AuditEventRecord(
                actor_admin_id=actor_id,
                action=action,
                target_type="system_scene",
                target_id=str(record.id),
                reason=reason,
                before_summary=before,
                after_summary=after,
                correlation_id=correlation_id or str(uuid.uuid4()),
            )
        )

    def create(
        self,
        admin: AdminContext,
        payload: SystemScenePayload | dict[str, Any],
        *,
        reason: str,
        correlation_id: str | None = None,
        seed_key: str | None = None,
    ) -> SystemSceneView:
        actor_id = self._admin_id(admin)
        scene = _validated_payload(payload)
        normalized_reason = self._reason(reason)
        with self.database.transaction(admin) as session:
            self._require_system_media(session, scene.cover_media_id)
            if seed_key:
                existing = session.scalar(
                    select(AssetRecord).where(
                        AssetRecord.scope == "system",
                        AssetRecord.asset_type == "scene",
                        AssetRecord.provenance["seed_key"].as_string() == seed_key,
                    )
                )
                if existing is not None:
                    return SystemSceneView(
                        record=existing,
                        payload=_validated_payload(existing.payload),
                        usage_count=self._usage_count(session, existing.id),
                    )
            record = AssetRecord(
                scope="system",
                asset_type="scene",
                user_id=None,
                workspace_id=None,
                project_id=None,
                series_id=None,
                media_object_id=scene.cover_media_id,
                name=scene.name,
                payload=_database_payload(
                    scene,
                    domain_id=f"system-scene-{uuid.uuid4().hex[:16]}",
                ),
                provenance={
                    "origin": "system_scene_catalog",
                    "seed_key": seed_key,
                    "created_by_admin_id": actor_id,
                    "updated_by_admin_id": actor_id,
                },
                schema_version=scene.schema_version,
                version=1,
            )
            session.add(record)
            session.flush()
            self._audit(
                session,
                actor_id=actor_id,
                record=record,
                action="system_scene.create",
                reason=normalized_reason,
                before=None,
                after={**_safe_audit_payload(scene), "version": 1},
                correlation_id=correlation_id,
            )
            session.flush()
            return SystemSceneView(record=record, payload=scene, usage_count=0)

    def get(
        self,
        identity: UserContext | AdminContext,
        scene_id: int,
        *,
        admin_view: bool,
    ) -> SystemSceneView:
        if admin_view:
            if not isinstance(identity, AdminContext):
                raise SystemSceneNotFoundError("请先登录系统管理后台")
            self._admin_id(identity)
        elif not isinstance(identity, UserContext) or not identity.user_id.strip():
            raise SystemSceneNotFoundError("请先登录")
        with self.database.transaction(identity) as session:
            record = self._scene_record(session, scene_id)
            payload = _validated_payload(record.payload)
            if not admin_view and (
                record.deleted_at is not None or payload.visibility != "enabled"
            ):
                raise SystemSceneNotFoundError("系统场景不存在")
            return SystemSceneView(
                record=record,
                payload=payload,
                usage_count=self._usage_count(session, record.id) if admin_view else 0,
            )

    def list_admin(
        self,
        admin: AdminContext,
        *,
        scene_id: int | None = None,
        query: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        visibility: str | None = None,
        lifecycle: str | None = None,
        schema_version: int | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> SystemScenePage:
        self._admin_id(admin)
        filters = [AssetRecord.scope == "system", AssetRecord.asset_type == "scene"]
        if scene_id is not None:
            filters.append(AssetRecord.id == scene_id)
        if query:
            filters.append(AssetRecord.name.ilike(f"%{query.strip()}%"))
        if category:
            filters.append(AssetRecord.payload["category"].as_string() == category.strip())
        if tag:
            normalized_tag = tag.strip()
            dialect_name = getattr(
                getattr(getattr(self.database, "engine", None), "dialect", None),
                "name",
                "",
            )
            if dialect_name == "postgresql":
                filters.append(AssetRecord.payload["tags"].contains([normalized_tag]))
            else:
                tag_values = func.json_each(
                    AssetRecord.payload,
                    "$.tags",
                ).table_valued("key", "value")
                filters.append(
                    select(1)
                    .select_from(tag_values)
                    .where(tag_values.c.value == normalized_tag)
                    .exists()
                )
        if visibility:
            filters.append(AssetRecord.payload["visibility"].as_string() == visibility)
        if lifecycle == "active":
            filters.append(AssetRecord.deleted_at.is_(None))
        elif lifecycle == "archived":
            filters.append(AssetRecord.deleted_at.is_not(None))
        if schema_version is not None:
            filters.append(AssetRecord.schema_version == schema_version)
        if start_at is not None:
            filters.append(AssetRecord.created_at >= start_at)
        if end_at is not None:
            filters.append(AssetRecord.created_at <= end_at)
        with self.database.transaction(admin) as session:
            total = int(
                session.scalar(select(func.count(AssetRecord.id)).where(*filters)) or 0
            )
            records = list(
                session.scalars(
                    select(AssetRecord)
                    .where(*filters)
                    .order_by(
                        AssetRecord.payload["sort_order"].as_integer().asc(),
                        AssetRecord.updated_at.desc(),
                        AssetRecord.id.asc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            return SystemScenePage(
                items=[
                    SystemSceneView(
                        record=record,
                        payload=_validated_payload(record.payload),
                        usage_count=self._usage_count(session, record.id),
                    )
                    for record in records
                ],
                total=total,
                offset=offset,
                limit=limit,
            )

    def list_enabled(self, identity: UserContext) -> list[SystemSceneView]:
        if not identity.user_id.strip():
            raise SystemSceneNotFoundError("请先登录")
        with self.database.transaction(identity) as session:
            records = list(
                session.scalars(
                    select(AssetRecord)
                    .where(
                        AssetRecord.scope == "system",
                        AssetRecord.asset_type == "scene",
                        AssetRecord.deleted_at.is_(None),
                        AssetRecord.payload["visibility"].as_string() == "enabled",
                    )
                    .order_by(
                        AssetRecord.payload["category"].as_string().asc(),
                        AssetRecord.payload["sort_order"].as_integer().asc(),
                        AssetRecord.id.asc(),
                    )
                )
            )
            return [
                SystemSceneView(
                    record=record,
                    payload=_validated_payload(record.payload),
                    usage_count=0,
                )
                for record in records
            ]

    def update(
        self,
        admin: AdminContext,
        scene_id: int,
        payload: SystemScenePayload | dict[str, Any],
        *,
        expected_version: int,
        reason: str,
        confirmed_usage_count: int | None = None,
        archive: bool = False,
        correlation_id: str | None = None,
    ) -> SystemSceneView:
        actor_id = self._admin_id(admin)
        scene = _validated_payload(payload)
        normalized_reason = self._reason(reason)
        with self.database.transaction(admin) as session:
            record = self._scene_record(session, scene_id, lock=True)
            if record.version != expected_version:
                raise SystemSceneConflictError(
                    "系统场景已被更新，请刷新后重试",
                    code="SYSTEM_SCENE_VERSION_CONFLICT",
                    details={"current_version": record.version},
                )
            current = _validated_payload(record.payload)
            usage_count = self._usage_count(session, record.id)
            removing_from_catalog = (
                current.visibility == "enabled" and scene.visibility == "disabled"
            ) or archive
            if removing_from_catalog and usage_count > 0 and confirmed_usage_count != usage_count:
                raise SystemSceneConflictError(
                    "该场景已被用户引用，请确认当前引用数量后重试",
                    code="SYSTEM_SCENE_REFERENCED",
                    details={"usage_count": usage_count},
                )
            self._require_system_media(session, scene.cover_media_id)
            before = {**_safe_audit_payload(current), "version": record.version}
            domain_id = str(record.payload.get("id") or f"system-scene-{record.id}")
            record.name = scene.name
            record.payload = _database_payload(scene, domain_id=domain_id)
            record.media_object_id = scene.cover_media_id
            record.schema_version = scene.schema_version
            record.version += 1
            record.provenance = {
                **dict(record.provenance),
                "updated_by_admin_id": actor_id,
            }
            if archive:
                record.deleted_at = datetime.now(UTC)
                record.retention_expires_at = None
            self._audit(
                session,
                actor_id=actor_id,
                record=record,
                action="system_scene.archive" if archive else "system_scene.update",
                reason=normalized_reason,
                before=before,
                after={
                    **_safe_audit_payload(scene),
                    "version": record.version,
                    "archived": record.deleted_at is not None,
                    "confirmed_usage_count": confirmed_usage_count,
                },
                correlation_id=correlation_id,
            )
            session.flush()
            return SystemSceneView(record=record, payload=scene, usage_count=usage_count)

    def copy_to_workspace(
        self,
        context: WorkspaceContext,
        scene_id: int,
        *,
        project_id: int | None = None,
    ) -> AssetRecord:
        user_id = parse_database_id(context.identity.user_id, field="用户 ID")
        workspace_id = parse_database_id(context.workspace_id, field="工作区 ID")
        with self.database.transaction(context.identity) as session:
            workspace = session.scalar(
                select(WorkspaceRecord.id).where(
                    WorkspaceRecord.id == workspace_id,
                    WorkspaceRecord.user_id == user_id,
                    WorkspaceRecord.deleted_at.is_(None),
                )
            )
            if workspace is None:
                raise SystemSceneNotFoundError("工作区不存在")
            if project_id is not None and session.scalar(
                select(ProjectRecord.id).where(
                    ProjectRecord.id == project_id,
                    ProjectRecord.user_id == user_id,
                    ProjectRecord.workspace_id == workspace_id,
                    ProjectRecord.deleted_at.is_(None),
                )
            ) is None:
                raise SystemSceneNotFoundError("项目不存在")
            source = self._scene_record(session, scene_id, lock=True)
            source_payload = _validated_payload(source.payload)
            if source.deleted_at is not None or source_payload.visibility != "enabled":
                raise SystemSceneNotFoundError("系统场景不存在")
            copied_payload = {
                "id": f"scene_{uuid.uuid4().hex[:12]}",
                "name": source_payload.name,
                "description": source_payload.description,
                "image_url": (
                    f"media:{source_payload.cover_media_id}"
                    if source_payload.cover_media_id
                    else None
                ),
                "video_prompt": source_payload.prompt,
                "time_of_day": None,
                "lighting_mood": source_payload.style,
            }
            record = AssetRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                project_id=project_id,
                series_id=None,
                media_object_id=source_payload.cover_media_id,
                scope="project" if project_id is not None else "workspace",
                asset_type="scene",
                name=source_payload.name,
                payload=copied_payload,
                provenance={
                    "origin": "system_scene_copy",
                    "source_system_scene_id": str(source.id),
                    "source_version": source.version,
                    "source_snapshot": source_payload.model_dump(mode="json"),
                    "source_media_id": source_payload.cover_media_id,
                },
                schema_version=1,
                version=1,
            )
            session.add(record)
            session.flush()
            return record

    def seed(
        self,
        admin: AdminContext,
        scenes: Iterable[tuple[str, SystemScenePayload | dict[str, Any]]],
    ) -> list[SystemSceneView]:
        return [
            self.create(
                admin,
                payload,
                reason="系统初始化内置场景",
                seed_key=seed_key,
            )
            for seed_key, payload in scenes
        ]

    def store_system_media(
        self,
        admin: AdminContext,
        *,
        content: bytes,
        mime_type: str,
        filename: str,
        reason: str,
    ) -> StoredSystemMedia:
        actor_id = self._admin_id(admin)
        normalized_reason = self._reason(reason)
        if self.media_storage is None:
            raise MediaStorageError("系统媒体存储尚未就绪")
        if not content or len(content) > self.MAX_COVER_BYTES:
            raise MediaValidationError("场景封面必须小于等于 10MB")
        media_rule = self.ALLOWED_COVER_TYPES.get(mime_type.lower())
        if media_rule is None or not media_rule[1](content):
            raise MediaValidationError("场景封面仅支持有效的 PNG、JPEG 或 WebP 图片")
        suffix = media_rule[0]
        checksum = hashlib.sha256(content).hexdigest()
        prefix = self.media_storage.namespace_prefix
        object_key = "/".join(
            part
            for part in (
                prefix,
                "system",
                "scenes",
                f"cover-{secrets.token_hex(16)}{suffix}",
            )
            if part
        )
        with self.database.transaction(admin) as session:
            record = MediaObjectRecord(
                scope="system",
                user_id=None,
                workspace_id=None,
                project_id=None,
                object_key=object_key,
                mime_type=mime_type.lower(),
                size_bytes=len(content),
                checksum_sha256=checksum,
                lifecycle_state="pending",
                provenance={
                    "origin": "system_scene_upload",
                    "created_by_admin_id": actor_id,
                    "filename": filename.rsplit("/", 1)[-1][:240],
                    "reason": normalized_reason,
                },
            )
            session.add(record)
            session.flush()
            media_id = record.id
        try:
            self.media_storage.object_store.put(object_key, content, mime_type.lower())
        except Exception as exc:
            with self.database.transaction(admin) as session:
                failed = session.get(MediaObjectRecord, media_id)
                if failed is not None:
                    failed.lifecycle_state = "failed"
            raise MediaStorageError("系统场景封面上传失败") from exc
        try:
            with self.database.transaction(admin) as session:
                active = session.get(MediaObjectRecord, media_id)
                if active is None or active.lifecycle_state != "pending":
                    raise MediaStorageError("系统场景封面状态异常")
                active.lifecycle_state = "active"
                session.add(
                    AuditEventRecord(
                        actor_admin_id=actor_id,
                        action="system_scene.media.upload",
                        target_type="system_media",
                        target_id=str(media_id),
                        reason=normalized_reason,
                        after_summary={
                            "mime_type": mime_type.lower(),
                            "size_bytes": len(content),
                            "checksum_sha256": checksum,
                        },
                        correlation_id=str(uuid.uuid4()),
                    )
                )
        except Exception:
            try:
                self.media_storage.object_store.delete(object_key)
            except Exception:
                pass
            raise
        return StoredSystemMedia(
            media_id=media_id,
            mime_type=mime_type.lower(),
            size_bytes=len(content),
            checksum_sha256=checksum,
        )

    def authorized_media_url(
        self,
        identity: UserContext,
        media_id: int,
        *,
        expires_seconds: int = 300,
    ) -> tuple[str, datetime]:
        if self.media_storage is None:
            raise MediaStorageError("系统媒体存储尚未就绪")
        if expires_seconds < 1 or expires_seconds > 900:
            raise MediaValidationError("媒体链接有效期必须在 15 分钟以内")
        with self.database.transaction(identity) as session:
            media = session.scalar(
                select(MediaObjectRecord).where(
                    MediaObjectRecord.id == media_id,
                    MediaObjectRecord.scope == "system",
                    MediaObjectRecord.lifecycle_state == "active",
                    MediaObjectRecord.deleted_at.is_(None),
                )
            )
            if media is None:
                raise SystemSceneNotFoundError("系统媒体不存在")
            allowed = session.scalar(
                select(AssetRecord.id)
                .where(
                    AssetRecord.media_object_id == media_id,
                    AssetRecord.deleted_at.is_(None),
                    (
                        (
                            (AssetRecord.scope == "system")
                            & (AssetRecord.asset_type == "scene")
                            & (
                                AssetRecord.payload["visibility"].as_string()
                                == "enabled"
                            )
                        )
                        | (AssetRecord.user_id == parse_database_id(identity.user_id))
                    ),
                )
                .limit(1)
            )
            if allowed is None:
                raise SystemSceneNotFoundError("系统媒体不存在")
            object_key = media.object_key
        try:
            url = self.media_storage.object_store.signed_get_url(
                object_key,
                expires_seconds,
            )
        except Exception as exc:
            raise MediaStorageError("系统媒体授权链接生成失败") from exc
        if not url:
            raise MediaStorageError("系统媒体授权链接生成失败")
        return url, datetime.now(UTC) + timedelta(seconds=expires_seconds)

    def authorized_admin_media_url(
        self,
        admin: AdminContext,
        media_id: int,
        *,
        expires_seconds: int = 300,
    ) -> tuple[str, datetime]:
        self._admin_id(admin)
        if self.media_storage is None:
            raise MediaStorageError("系统媒体存储尚未就绪")
        if expires_seconds < 1 or expires_seconds > 900:
            raise MediaValidationError("媒体链接有效期必须在 15 分钟以内")
        with self.database.transaction(admin) as session:
            media = session.scalar(
                select(MediaObjectRecord).where(
                    MediaObjectRecord.id == media_id,
                    MediaObjectRecord.scope == "system",
                    MediaObjectRecord.lifecycle_state == "active",
                    MediaObjectRecord.deleted_at.is_(None),
                )
            )
            if media is None:
                raise SystemSceneNotFoundError("系统媒体不存在")
            object_key = media.object_key
        try:
            url = self.media_storage.object_store.signed_get_url(
                object_key,
                expires_seconds,
            )
        except Exception as exc:
            raise MediaStorageError("系统媒体授权链接生成失败") from exc
        if not url:
            raise MediaStorageError("系统媒体授权链接生成失败")
        return url, datetime.now(UTC) + timedelta(seconds=expires_seconds)
