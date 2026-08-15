from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, update

from src.apps.comic_gen.models import GlobalAssetLibrary, Script, Series
from src.apps.playground.models import PlaygroundGeneration

from .asset_repositories import (
    AssetConflictError,
    PostgresProjectAssetRepository,
    PostgresSeriesAssetRepository,
    PostgresWorkspaceAssetRepository,
)
from .auth.admin import AdminAuthorizationError
from .content_repositories import (
    DocumentPayloadValidationError,
    PostgresProjectRepository,
    PostgresSeriesRepository,
)
from .contracts import MediaWrite, UserContext, WorkspaceContext
from .database import Database
from .db_models import (
    AITaskRecord,
    AssetRecord,
    AuditEventRecord,
    ImportBatchItemRecord,
    ImportBatchRecord,
    ImportedPlaygroundHistoryRecord,
    MediaObjectRecord,
    ProjectRecord,
    SeriesRecord,
    UserRecord,
    WorkspaceRecord,
)
from .media_storage import CloudMediaStorage
from .identifiers import parse_database_id


MissingMediaPolicy = Literal["reject", "clear"]
ITEM_ORDER = {
    "media": 0,
    "series": 1,
    "project": 2,
    "asset": 3,
    "playground_history": 4,
}
MEDIA_FIELD_NAMES = {
    "audio_url",
    "avatar_url",
    "bg_audio_source_video",
    "bg_audio_url",
    "bgm_url",
    "dubbed_video_url",
    "full_body_image_url",
    "headshot_image_url",
    "image_url",
    "input_media",
    "media_path",
    "preview_video_url",
    "reference_image_url",
    "reference_image_urls",
    "reference_video_urls",
    "rendered_image_url",
    "sfx_url",
    "source_audio_url",
    "t2i_image_urls",
    "thumbnail_path",
    "three_view_image_url",
    "url",
    "video_url",
}


class LocalImportError(RuntimeError):
    pass


class LocalImportValidationError(LocalImportError):
    pass


class LocalImportConflictError(LocalImportError):
    pass


@dataclass(frozen=True, slots=True)
class ImportIssue:
    code: str
    message: str
    source_key: str
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source_key": self.source_key,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class SourceItem:
    item_type: str
    source_key: str
    checksum: str
    payload: Any
    target_id: str | None = None
    scope: str | None = None
    parent_id: str | None = None
    asset_type: str | None = None


@dataclass(frozen=True, slots=True)
class SourceMedia:
    source_key: str
    path: Path
    checksum: str
    size_bytes: int
    mime_type: str


@dataclass(slots=True)
class ImportManifest:
    source_root: Path
    fingerprint: str
    items: list[SourceItem]
    media: dict[str, SourceMedia]
    reference_sources: dict[str, str | None]
    reference_targets: dict[str, str | None]
    issues: list[ImportIssue] = field(default_factory=list)

    @property
    def total_media_bytes(self) -> int:
        return sum(item.size_bytes for item in self.media.values())

    @property
    def ready(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.item_type] = counts.get(item.item_type, 0) + 1
        return counts


def _canonical_json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        return None


def _payload_media_references(value: Any, field_name: str | None = None):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _payload_media_references(item, str(key))
    elif isinstance(value, list):
        for item in value:
            yield from _payload_media_references(item, field_name)
    elif isinstance(value, str) and field_name in MEDIA_FIELD_NAMES and value.strip():
        yield value.strip()


class LocalSourceDiscovery:
    def __init__(self, allowed_root: str | os.PathLike[str], *, max_import_bytes: int) -> None:
        self.allowed_root = Path(allowed_root).expanduser().resolve()
        self.max_import_bytes = max_import_bytes
        if max_import_bytes <= 0:
            raise ValueError("导入容量限制必须为正整数")

    def resolve_source(self, relative_source: str) -> Path:
        if not relative_source.strip():
            raise LocalImportValidationError("请选择本地导入目录")
        candidate = (self.allowed_root / relative_source).resolve()
        if candidate != self.allowed_root and self.allowed_root not in candidate.parents:
            raise LocalImportValidationError("导入目录超出管理员配置的允许范围")
        if not candidate.is_dir():
            raise LocalImportValidationError("本地导入目录不存在")
        return candidate

    @staticmethod
    def _data_file(root: Path, name: str) -> Path | None:
        candidates = (root / "output" / name, root / name)
        return next((path for path in candidates if path.is_file()), None)

    @staticmethod
    def _load_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LocalImportValidationError(f"无法读取本地数据文件：{path.name}") from exc

    @staticmethod
    def _relative_key(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()

    def _resolve_media(self, root: Path, reference: str) -> tuple[str, Path] | None:
        parsed = urlparse(reference)
        if parsed.scheme in {"http", "https"}:
            if parsed.hostname not in {"localhost", "127.0.0.1"}:
                return None
            raw_path = unquote(parsed.path)
        elif parsed.scheme:
            return None
        else:
            raw_path = unquote(reference.split("?", 1)[0].split("#", 1)[0])
        if raw_path.startswith("/files/"):
            raw_path = raw_path[len("/files/") :]
        raw_path = raw_path.lstrip("/")
        candidates = [root / raw_path, root / "output" / raw_path]
        if root.name == "output":
            candidates.append(root / raw_path.removeprefix("output/"))
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved != root and root not in resolved.parents:
                continue
            if resolved.is_file():
                return self._relative_key(root, resolved), resolved
        return None

    def discover(
        self,
        relative_source: str,
        *,
        include_playground: bool,
        missing_media_policy: MissingMediaPolicy,
    ) -> ImportManifest:
        root = self.resolve_source(relative_source)
        issues: list[ImportIssue] = []
        items: list[SourceItem] = []
        payloads: list[Any] = []

        series_by_id: dict[str, Series] = {}
        series_file = self._data_file(root, "series.json")
        if series_file is not None:
            raw_series = self._load_json(series_file)
            if not isinstance(raw_series, dict):
                raise LocalImportValidationError("系列数据文件必须是对象")
            for source_id, payload in raw_series.items():
                try:
                    series = Series.model_validate(payload)
                except ValidationError as exc:
                    issues.append(ImportIssue("SERIES_INVALID", "系列数据格式无效", f"series:{source_id}"))
                    continue
                if series.id != str(source_id) or _safe_uuid(series.id) is None:
                    issues.append(ImportIssue("SERIES_ID_INVALID", "系列标识无效或与索引不一致", f"series:{source_id}"))
                    continue
                series_by_id[series.id] = series
                payloads.append(series.model_dump(mode="json"))
                items.append(SourceItem("series", f"series:{series.id}", _checksum(series), series, series.id))

        projects_by_id: dict[str, Script] = {}
        projects_file = self._data_file(root, "projects.json")
        if projects_file is not None:
            raw_projects = self._load_json(projects_file)
            if not isinstance(raw_projects, dict):
                raise LocalImportValidationError("项目数据文件必须是对象")
            for source_id, payload in raw_projects.items():
                try:
                    project = Script.model_validate(payload)
                except ValidationError:
                    issues.append(ImportIssue("PROJECT_INVALID", "项目数据格式无效", f"project:{source_id}"))
                    continue
                if project.id != str(source_id) or _safe_uuid(project.id) is None:
                    issues.append(ImportIssue("PROJECT_ID_INVALID", "项目标识无效或与索引不一致", f"project:{source_id}"))
                    continue
                if project.series_id and project.series_id not in series_by_id:
                    issues.append(ImportIssue("SERIES_REFERENCE_MISSING", "项目关联的系列不在本次导入源中", f"project:{project.id}"))
                projects_by_id[project.id] = project
                payloads.append(project.model_dump(mode="json"))
                items.append(SourceItem("project", f"project:{project.id}", _checksum(project), project, project.id))

        for series in series_by_id.values():
            missing = [project_id for project_id in series.episode_ids if project_id not in projects_by_id]
            if missing:
                issues.append(ImportIssue("EPISODE_REFERENCE_MISSING", "系列包含不存在的项目标识", f"series:{series.id}"))
            self._append_assets(items, payloads, "series", series.id, series)
        for project in projects_by_id.values():
            self._append_assets(items, payloads, "project", project.id, project)

        library_file = self._data_file(root, "library_assets.json")
        if library_file is not None:
            try:
                library = GlobalAssetLibrary.model_validate(self._load_json(library_file))
            except ValidationError as exc:
                raise LocalImportValidationError("工作区资产库数据格式无效") from exc
            payloads.append(library.model_dump(mode="json"))
            self._append_assets(items, payloads, "workspace", None, library)

        if include_playground:
            history_file = self._data_file(root, "playground_history.json")
            if history_file is not None:
                raw_history = self._load_json(history_file)
                if not isinstance(raw_history, list):
                    raise LocalImportValidationError("Playground 历史文件必须是数组")
                for index, payload in enumerate(raw_history):
                    try:
                        generation = PlaygroundGeneration.model_validate(payload)
                    except ValidationError:
                        issues.append(ImportIssue("PLAYGROUND_INVALID", "Playground 历史数据格式无效", f"playground:{index}"))
                        continue
                    payloads.append(generation.model_dump(mode="json"))
                    items.append(SourceItem("playground_history", f"playground:{generation.id}", _checksum(generation), generation, generation.id))

        references = sorted({reference for payload in payloads for reference in _payload_media_references(payload)})
        media: dict[str, SourceMedia] = {}
        reference_keys: dict[str, str | None] = {}
        for reference in references:
            if reference.startswith("media:"):
                issues.append(ImportIssue("MEDIA_ID_NOT_LOCAL", "本地数据包含无法从源目录验证的媒体标识", reference, missing_media_policy == "reject"))
                reference_keys[reference] = None
                continue
            resolved = self._resolve_media(root, reference)
            if resolved is None:
                issues.append(ImportIssue("MEDIA_REFERENCE_MISSING", "媒体引用不存在或不是允许的本地文件", reference, missing_media_policy == "reject"))
                reference_keys[reference] = None
                continue
            source_key, path = resolved
            reference_keys[reference] = source_key
            if source_key not in media:
                size = path.stat().st_size
                mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                media[source_key] = SourceMedia(source_key, path, _file_checksum(path), size, mime_type)

        for source in media.values():
            items.append(SourceItem("media", f"media:{source.source_key}", source.checksum, source, None))
        total_bytes = sum(source.size_bytes for source in media.values())
        if total_bytes > self.max_import_bytes:
            issues.append(ImportIssue("IMPORT_CAPACITY_EXCEEDED", "媒体总容量超过本次导入限制", "media", True))

        manifest_material = [
            {"type": item.item_type, "key": item.source_key, "checksum": item.checksum}
            for item in sorted(items, key=lambda item: (ITEM_ORDER[item.item_type], item.source_key))
        ]
        fingerprint = _checksum(manifest_material)
        reference_targets = {reference: None for reference in reference_keys}
        return ImportManifest(
            root,
            fingerprint,
            items,
            media,
            reference_keys,
            reference_targets,
            issues,
        )

    @staticmethod
    def _append_assets(
        items: list[SourceItem],
        payloads: list[Any],
        scope: str,
        parent_id: str | None,
        container: Any,
    ) -> None:
        collections = (
            ("character", getattr(container, "characters", [])),
            ("scene", getattr(container, "scenes", [])),
            ("prop", getattr(container, "props", [])),
            ("voice", getattr(container, "custom_voices", [])),
        )
        seen: set[tuple[str, str]] = set()
        for asset_type, assets in collections:
            for asset in assets:
                domain_id = str(asset.id)
                duplicate_key = (asset_type, domain_id)
                source_key = f"asset:{scope}:{parent_id or 'workspace'}:{asset_type}:{domain_id}"
                if duplicate_key in seen:
                    continue
                seen.add(duplicate_key)
                payloads.append(asset.model_dump(mode="json"))
                items.append(SourceItem("asset", source_key, _checksum(asset), asset, domain_id, scope, parent_id, asset_type))


class LocalImportService:
    RETENTION_DAYS = 30

    def __init__(
        self,
        database: Database,
        media_storage: CloudMediaStorage,
        *,
        allowed_root: str | os.PathLike[str],
        max_import_bytes: int = 10 * 1024 * 1024 * 1024,
    ) -> None:
        self.database = database
        self.media_storage = media_storage
        self.discovery = LocalSourceDiscovery(allowed_root, max_import_bytes=max_import_bytes)
        self.projects = PostgresProjectRepository(database)
        self.series = PostgresSeriesRepository(database)
        self.asset_repositories = {
            "project": PostgresProjectAssetRepository(database),
            "series": PostgresSeriesAssetRepository(database),
            "workspace": PostgresWorkspaceAssetRepository(database),
        }

    @staticmethod
    def _require_admin(identity: UserContext) -> None:
        if not identity.is_platform_admin:
            raise AdminAuthorizationError("仅平台管理员可以执行本地数据导入")

    def _target_context(
        self,
        admin: UserContext,
        target_user_id: str,
        target_workspace_id: str,
    ) -> WorkspaceContext:
        self._require_admin(admin)
        try:
            target_user = parse_database_id(target_user_id, field="目标用户 ID")
            target_workspace = parse_database_id(
                target_workspace_id,
                field="目标工作区 ID",
            )
        except ValueError:
            raise LocalImportValidationError("目标用户或工作区标识无效")
        with self.database.transaction(admin) as session:
            user = session.scalar(select(UserRecord).where(UserRecord.id == target_user, UserRecord.status == "active"))
            workspace = session.scalar(
                select(WorkspaceRecord).where(
                    WorkspaceRecord.id == target_workspace,
                    WorkspaceRecord.user_id == target_user,
                    WorkspaceRecord.deleted_at.is_(None),
                )
            )
            if user is None or workspace is None:
                raise LocalImportValidationError("目标用户与工作区归属不匹配或不可用")
        return WorkspaceContext(UserContext(user_id=target_user_id), target_workspace_id)

    def dry_run(
        self,
        admin: UserContext,
        *,
        target_user_id: str,
        target_workspace_id: str,
        source_directory: str,
        include_playground: bool = True,
        missing_media_policy: MissingMediaPolicy = "reject",
    ) -> dict[str, Any]:
        context = self._target_context(admin, target_user_id, target_workspace_id)
        target_user = parse_database_id(context.identity.user_id, field="目标用户 ID")
        target_workspace = parse_database_id(context.workspace_id, field="目标工作区 ID")
        manifest = self.discovery.discover(
            source_directory,
            include_playground=include_playground,
            missing_media_policy=missing_media_policy,
        )
        self._append_target_conflicts(context, manifest, allow_matching=True)
        report = self._dry_run_report(manifest)
        options = {
            "source_directory": source_directory,
            "include_playground": include_playground,
            "missing_media_policy": missing_media_policy,
        }
        with self.database.transaction(admin) as session:
            batch = session.scalar(
                select(ImportBatchRecord).where(
                    ImportBatchRecord.target_user_id == target_user,
                    ImportBatchRecord.target_workspace_id == target_workspace,
                    ImportBatchRecord.source_fingerprint == manifest.fingerprint,
                )
            )
            if batch is None:
                batch = ImportBatchRecord(
                    actor_admin_user_id=parse_database_id(
                        admin.user_id,
                        field="管理员 ID",
                    ),
                    target_user_id=target_user,
                    target_workspace_id=target_workspace,
                    source_fingerprint=manifest.fingerprint,
                    status="dry_run",
                    options=options,
                    dry_run_report=report,
                )
                session.add(batch)
                session.flush()
                for item in manifest.items:
                    session.add(
                        ImportBatchItemRecord(
                            batch_id=batch.id,
                            user_id=target_user,
                            workspace_id=target_workspace,
                            item_type=item.item_type,
                            source_key=item.source_key,
                            source_checksum=item.checksum,
                            target_type=item.item_type,
                            target_id=self._planned_target_id(item, manifest),
                            detail={
                                "scope": item.scope,
                                "parent_id": item.parent_id,
                                "asset_type": item.asset_type,
                            },
                        )
                    )
            else:
                if batch.options != options:
                    raise LocalImportConflictError("相同导入源已使用不同选项建立批次")
                batch.dry_run_report = report
                if batch.status not in {"running", "completed", "reverted"}:
                    batch.status = "dry_run"
            batch_id = str(batch.id)
        return {"batch_id": batch_id, **report}

    def execute(self, admin: UserContext, batch_id: str) -> dict[str, Any]:
        self._require_admin(admin)
        try:
            batch_database_id = parse_database_id(batch_id, field="导入批次 ID")
        except ValueError:
            raise LocalImportValidationError("导入批次标识无效")
        with self.database.transaction(admin) as session:
            batch = session.get(ImportBatchRecord, batch_database_id)
            if batch is None:
                raise LocalImportValidationError("导入批次不存在")
            if batch.status == "reverted":
                raise LocalImportConflictError("已回滚批次不能再次执行")
            options = dict(batch.options)
            target_user_id = str(batch.target_user_id)
            target_workspace_id = str(batch.target_workspace_id)
            expected_fingerprint = batch.source_fingerprint
        context = self._target_context(admin, target_user_id, target_workspace_id)
        manifest = self.discovery.discover(
            str(options["source_directory"]),
            include_playground=bool(options.get("include_playground", True)),
            missing_media_policy=str(options.get("missing_media_policy", "reject")),
        )
        self._append_target_conflicts(context, manifest, allow_matching=True)
        if manifest.fingerprint != expected_fingerprint:
            raise LocalImportConflictError("本地导入源在预检后发生变化，请重新预检")
        if not manifest.ready:
            raise LocalImportValidationError("预检仍有阻断问题，不能开始导入")

        with self.database.transaction(admin) as session:
            batch = session.get(ImportBatchRecord, batch_database_id)
            batch.status = "running"
            batch.started_at = batch.started_at or datetime.now(UTC)
            batch.error_report = None
            session.execute(
                update(ImportBatchItemRecord)
                .where(
                    ImportBatchItemRecord.batch_id == batch_database_id,
                    ImportBatchItemRecord.status.in_(("running", "failed")),
                )
                .values(status="pending")
            )

        item_lookup = {(item.item_type, item.source_key): item for item in manifest.items}
        ordered_keys = sorted(item_lookup, key=lambda key: (ITEM_ORDER[key[0]], key[1]))
        failures: list[dict[str, str]] = []
        for key in ordered_keys:
            try:
                self._execute_item(
                    admin,
                    context,
                    batch_database_id,
                    manifest,
                    item_lookup[key],
                )
            except Exception as exc:  # noqa: BLE001 - each item remains resumable.
                self._mark_item_failed(admin, batch_database_id, key, exc)
                failures.append({"source_key": key[1], "message": str(exc)})
                break

        if not failures:
            self._finalize_series_documents(
                admin,
                context,
                batch_database_id,
                manifest,
            )
        result = self._reconcile(admin, context, batch_database_id, manifest)
        with self.database.transaction(admin) as session:
            batch = session.get(ImportBatchRecord, batch_database_id)
            batch.result_report = result
            batch.completed_at = datetime.now(UTC)
            if failures or not result["integrity_ok"]:
                batch.status = "failed"
                batch.error_report = {"message": "导入未完整完成，可修复后重试", "items": failures}
            else:
                batch.status = "completed"
                batch.error_report = None
            final_status = batch.status
        return {"batch_id": batch_id, "status": final_status, **result}

    def get_batch(self, admin: UserContext, batch_id: str) -> dict[str, Any]:
        self._require_admin(admin)
        try:
            batch_database_id = parse_database_id(batch_id, field="导入批次 ID")
        except ValueError:
            raise LocalImportValidationError("导入批次标识无效")
        with self.database.transaction(admin) as session:
            batch = session.get(ImportBatchRecord, batch_database_id)
            if batch is None:
                raise LocalImportValidationError("导入批次不存在")
            statuses = dict(
                session.execute(
                    select(ImportBatchItemRecord.status, func.count(ImportBatchItemRecord.id))
                    .where(ImportBatchItemRecord.batch_id == batch_database_id)
                    .group_by(ImportBatchItemRecord.status)
                ).all()
            )
            return {
                "id": str(batch.id),
                "status": batch.status,
                "source_fingerprint": batch.source_fingerprint,
                "target_user_id": str(batch.target_user_id),
                "target_workspace_id": str(batch.target_workspace_id),
                "item_status_counts": {str(key): int(value) for key, value in statuses.items()},
                "dry_run_report": batch.dry_run_report,
                "result_report": batch.result_report,
                "error_report": batch.error_report,
                "created_at": batch.created_at.isoformat(),
                "started_at": batch.started_at.isoformat() if batch.started_at else None,
                "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
                "reverted_at": batch.reverted_at.isoformat() if batch.reverted_at else None,
                "rollback_reason": batch.rollback_reason,
            }

    def rollback(self, admin: UserContext, batch_id: str, reason: str) -> dict[str, Any]:
        self._require_admin(admin)
        if not reason.strip():
            raise LocalImportValidationError("回滚必须填写原因")
        try:
            batch_database_id = parse_database_id(batch_id, field="导入批次 ID")
        except ValueError:
            raise LocalImportValidationError("导入批次标识无效")
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=self.RETENTION_DAYS)
        reverted = 0
        preserved = 0
        with self.database.transaction(admin) as session:
            batch = session.get(ImportBatchRecord, batch_database_id)
            if batch is None:
                raise LocalImportValidationError("导入批次不存在")
            if batch.status not in {"completed", "failed"}:
                raise LocalImportConflictError("当前批次状态不允许回滚")
            items = list(
                session.scalars(
                    select(ImportBatchItemRecord)
                    .where(
                        ImportBatchItemRecord.batch_id == batch_database_id,
                        ImportBatchItemRecord.created_by_batch.is_(True),
                        ImportBatchItemRecord.status == "completed",
                    )
                )
            )
            rollback_order = {
                "project": 0,
                "asset": 1,
                "playground_history": 2,
                "series": 3,
                "media": 4,
            }
            items.sort(key=lambda item: (rollback_order.get(item.item_type, 99), item.source_key))
            for item in items:
                if self._rollback_item(session, batch, item, now, expires_at):
                    item.status = "reverted"
                    reverted += 1
                else:
                    preserved += 1
            batch.status = "reverted"
            batch.reverted_at = now
            batch.rollback_reason = reason.strip()
            batch.result_report = {
                **dict(batch.result_report or {}),
                "rollback": {
                    "message": "导入批次已回滚",
                    "reverted_items": reverted,
                    "preserved_items": preserved,
                },
            }
            session.add(
                AuditEventRecord(
                    actor_user_id=parse_database_id(
                        admin.user_id,
                        field="管理员 ID",
                    ),
                    target_user_id=batch.target_user_id,
                    workspace_id=batch.target_workspace_id,
                    action="import.rollback",
                    target_type="import_batch",
                    target_id=str(batch.id),
                    reason=reason.strip(),
                    after_summary={"reverted_items": reverted, "preserved_items": preserved},
                    correlation_id=str(uuid.uuid4()),
                )
            )
        return {"message": "导入批次已回滚", "reverted_items": reverted, "preserved_items": preserved}

    def _append_target_conflicts(
        self,
        context: WorkspaceContext,
        manifest: ImportManifest,
        *,
        allow_matching: bool = False,
    ) -> None:
        del context, manifest, allow_matching

    @staticmethod
    def _dry_run_report(manifest: ImportManifest) -> dict[str, Any]:
        return {
            "message": "预检通过，可以开始导入" if manifest.ready else "预检发现阻断问题，请处理后重试",
            "ready": manifest.ready,
            "source_fingerprint": manifest.fingerprint,
            "planned_counts": manifest.counts(),
            "media_bytes": manifest.total_media_bytes,
            "issues": [issue.as_dict() for issue in manifest.issues],
        }

    @staticmethod
    def _planned_target_id(item: SourceItem, manifest: ImportManifest) -> str | None:
        del item, manifest
        return None

    def _execute_item(
        self,
        admin: UserContext,
        context: WorkspaceContext,
        batch_id: int,
        manifest: ImportManifest,
        item: SourceItem,
    ) -> None:
        with self.database.transaction(admin) as session:
            tracked = session.scalar(
                select(ImportBatchItemRecord).where(
                    ImportBatchItemRecord.batch_id == batch_id,
                    ImportBatchItemRecord.item_type == item.item_type,
                    ImportBatchItemRecord.source_key == item.source_key,
                )
            )
            if tracked is None or tracked.source_checksum != item.checksum:
                raise LocalImportConflictError("导入明细与预检结果不一致")
            if tracked.status in {"completed", "reused"}:
                return
            tracked.status = "running"

        if item.item_type == "media":
            self._execute_media(admin, context, batch_id, manifest, item)
            return

        with self.database.transaction(admin) as session:
            tracked = session.scalar(
                select(ImportBatchItemRecord).where(
                    ImportBatchItemRecord.batch_id == batch_id,
                    ImportBatchItemRecord.item_type == item.item_type,
                    ImportBatchItemRecord.source_key == item.source_key,
                )
            )
            created, target_id = self._persist_nonmedia_item(session, context, batch_id, manifest, item)
            tracked.target_id = target_id
            tracked.created_by_batch = created
            tracked.status = "completed" if created else "reused"

    @staticmethod
    def _content_target_id(
        session,
        batch_id: int,
        item_type: str,
        source_id: str,
    ) -> int:
        target_id = session.scalar(
            select(ImportBatchItemRecord.target_id).where(
                ImportBatchItemRecord.batch_id == batch_id,
                ImportBatchItemRecord.item_type == item_type,
                ImportBatchItemRecord.source_key == f"{item_type}:{source_id}",
                ImportBatchItemRecord.status.in_({"completed", "reused"}),
            )
        )
        try:
            return parse_database_id(target_id, field=f"{item_type} 目标 ID")
        except ValueError as exc:
            raise LocalImportConflictError(
                f"导入内容缺少已建立的 {item_type} 标识映射"
            ) from exc

    def _persist_nonmedia_item(
        self,
        session,
        context: WorkspaceContext,
        batch_id: int,
        manifest: ImportManifest,
        item: SourceItem,
    ) -> tuple[bool, str]:
        transformed = self._transform_payload(item.payload, manifest.reference_targets)
        if item.item_type in {"series", "project"}:
            document_type = Series if item.item_type == "series" else Script
            if not isinstance(transformed, dict):
                raise LocalImportValidationError("导入内容格式无效")
            transformed = dict(transformed)
            if item.item_type == "series":
                transformed["episode_ids"] = []
            else:
                source_series_id = transformed.get("series_id")
                if source_series_id:
                    transformed["series_id"] = str(
                        self._content_target_id(
                            session,
                            batch_id,
                            "series",
                            str(source_series_id),
                        )
                    )
            document = document_type.model_validate(transformed)
            repository = self.series if item.item_type == "series" else self.projects
            stored = repository.add(context, document, session=session)
            return True, str(stored.document.id)
        if item.item_type == "asset":
            repository = self.asset_repositories[item.scope]
            parent_id = (
                str(
                    self._content_target_id(
                        session,
                        batch_id,
                        str(item.scope),
                        str(item.parent_id),
                    )
                )
                if item.scope != "workspace"
                else None
            )
            document = transformed
            existing = repository.find_by_domain_id(context, parent_id, item.asset_type, item.target_id, session=session)
            if existing is not None:
                if _checksum(existing.document) != _checksum(document):
                    raise AssetConflictError("目标作用域已有不同内容的同标识资产")
                return False, existing.record_id
            media_id = self._first_media_id(document)
            stored = repository.add(
                context,
                parent_id,
                item.asset_type,
                document,
                provenance={"origin": "local_import", "import_batch_id": str(batch_id), "source_key": item.source_key},
                media_object_id=media_id,
                session=session,
            )
            return True, stored.record_id
        if item.item_type == "playground_history":
            source_id = str(item.target_id)
            existing = session.scalar(
                select(ImportedPlaygroundHistoryRecord).where(
                    ImportedPlaygroundHistoryRecord.user_id
                    == parse_database_id(context.identity.user_id, field="用户 ID"),
                    ImportedPlaygroundHistoryRecord.workspace_id
                    == parse_database_id(context.workspace_id, field="工作区 ID"),
                    ImportedPlaygroundHistoryRecord.source_id == source_id,
                )
            )
            if existing is not None:
                if existing.source_checksum != item.checksum:
                    raise LocalImportConflictError("Playground 历史标识已被不同内容占用")
                return False, str(existing.id)
            record = ImportedPlaygroundHistoryRecord(
                user_id=parse_database_id(context.identity.user_id, field="用户 ID"),
                workspace_id=parse_database_id(context.workspace_id, field="工作区 ID"),
                import_batch_id=batch_id,
                source_id=source_id,
                payload=transformed,
                source_checksum=item.checksum,
            )
            session.add(record)
            session.flush()
            return True, str(record.id)
        raise LocalImportValidationError("不支持的导入明细类型")

    def _execute_media(
        self,
        admin: UserContext,
        context: WorkspaceContext,
        batch_id: int,
        manifest: ImportManifest,
        item: SourceItem,
    ) -> None:
        source_key = item.source_key.removeprefix("media:")
        source = manifest.media[source_key]
        stored = self.media_storage.store_imported(
            context,
            MediaWrite(
                content=source.path.read_bytes(),
                content_type=source.mime_type,
                filename=source.path.name,
                provenance={"import_batch_id": str(batch_id)},
            ),
            source_fingerprint=manifest.fingerprint,
            source_key=source_key,
        )
        with self.database.transaction(admin) as session:
            tracked = session.scalar(
                select(ImportBatchItemRecord).where(
                    ImportBatchItemRecord.batch_id == batch_id,
                    ImportBatchItemRecord.item_type == "media",
                    ImportBatchItemRecord.source_key == item.source_key,
                )
            )
            record = session.get(
                MediaObjectRecord,
                parse_database_id(stored.media_id, field="媒体 ID"),
            )
            tracked.target_id = stored.media_id
            tracked.created_by_batch = bool(
                record and str(record.provenance.get("import_batch_id")) == str(batch_id)
            )
            tracked.status = "completed" if tracked.created_by_batch else "reused"
        for reference, mapped_source_key in manifest.reference_sources.items():
            if mapped_source_key == source_key:
                manifest.reference_targets[reference] = stored.media_id

    def _finalize_series_documents(
        self,
        admin: UserContext,
        context: WorkspaceContext,
        batch_id: int,
        manifest: ImportManifest,
    ) -> None:
        user_id = parse_database_id(context.identity.user_id, field="用户 ID")
        workspace_id = parse_database_id(context.workspace_id, field="工作区 ID")
        with self.database.transaction(admin) as session:
            for item in manifest.items:
                if item.item_type != "series" or not item.target_id:
                    continue
                target_id = self._content_target_id(
                    session,
                    batch_id,
                    "series",
                    item.target_id,
                )
                record = session.scalar(
                    select(SeriesRecord).where(
                        SeriesRecord.id == target_id,
                        SeriesRecord.user_id == user_id,
                        SeriesRecord.workspace_id == workspace_id,
                    )
                )
                if record is None:
                    raise LocalImportConflictError("导入系列记录不存在")
                transformed = self._transform_payload(
                    item.payload,
                    manifest.reference_targets,
                )
                if not isinstance(transformed, dict):
                    raise LocalImportValidationError("导入系列格式无效")
                payload = dict(transformed)
                payload["id"] = str(target_id)
                payload["episode_ids"] = [
                    str(
                        self._content_target_id(
                            session,
                            batch_id,
                            "project",
                            str(source_project_id),
                        )
                    )
                    for source_project_id in payload.get("episode_ids", [])
                ]
                validated = Series.model_validate(payload)
                record.title = validated.title
                record.payload = validated.model_dump(mode="json")

    @staticmethod
    def _transform_payload(value: Any, reference_targets: Mapping[str, str | None], field_name: str | None = None) -> Any:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {key: LocalImportService._transform_payload(item, reference_targets, str(key)) for key, item in value.items()}
        if isinstance(value, list):
            transformed = [LocalImportService._transform_payload(item, reference_targets, field_name) for item in value]
            return [item for item in transformed if item is not None]
        if isinstance(value, str) and field_name in MEDIA_FIELD_NAMES and value.strip() in reference_targets:
            media_id = reference_targets[value.strip()]
            return f"media:{media_id}" if media_id else None
        return value

    @staticmethod
    def _first_media_id(payload: Any) -> str | None:
        if isinstance(payload, Mapping):
            for value in payload.values():
                found = LocalImportService._first_media_id(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = LocalImportService._first_media_id(value)
                if found:
                    return found
        elif isinstance(payload, str) and payload.startswith("media:"):
            return payload.removeprefix("media:")
        return None

    def _mark_item_failed(self, admin: UserContext, batch_id: int, key: tuple[str, str], exc: Exception) -> None:
        with self.database.transaction(admin) as session:
            item = session.scalar(
                select(ImportBatchItemRecord).where(
                    ImportBatchItemRecord.batch_id == batch_id,
                    ImportBatchItemRecord.item_type == key[0],
                    ImportBatchItemRecord.source_key == key[1],
                )
            )
            if item is not None:
                item.status = "failed"
                item.detail = {**dict(item.detail or {}), "error": str(exc)[:500]}

    def _reconcile(
        self,
        admin: UserContext,
        context: WorkspaceContext,
        batch_id: int,
        manifest: ImportManifest,
    ) -> dict[str, Any]:
        with self.database.transaction(admin) as session:
            items = list(session.scalars(select(ImportBatchItemRecord).where(ImportBatchItemRecord.batch_id == batch_id)))
            status_counts: dict[str, int] = {}
            type_counts: dict[str, int] = {}
            checksums: list[str] = []
            for item in items:
                status_counts[item.status] = status_counts.get(item.status, 0) + 1
                if item.status in {"completed", "reused"}:
                    type_counts[item.item_type] = type_counts.get(item.item_type, 0) + 1
                    checksums.append(item.source_checksum)
            reference_errors = self._reference_errors(session, context)
        expected = manifest.counts()
        integrity_ok = type_counts == expected and not reference_errors and not status_counts.get("failed")
        return {
            "message": "导入完成并通过完整性核对" if integrity_ok else "导入尚未通过完整性核对",
            "integrity_ok": integrity_ok,
            "expected_counts": expected,
            "actual_counts": type_counts,
            "item_status_counts": status_counts,
            "reconciliation_checksum": _checksum(sorted(checksums)),
            "reference_errors": reference_errors,
        }

    @staticmethod
    def _reference_errors(session, context: WorkspaceContext) -> list[str]:
        user_id = parse_database_id(context.identity.user_id, field="用户 ID")
        workspace_id = parse_database_id(context.workspace_id, field="工作区 ID")
        series_ids = set(session.scalars(select(SeriesRecord.id).where(SeriesRecord.user_id == user_id, SeriesRecord.workspace_id == workspace_id, SeriesRecord.deleted_at.is_(None))))
        media_ids = set(session.scalars(select(MediaObjectRecord.id).where(MediaObjectRecord.user_id == user_id, MediaObjectRecord.workspace_id == workspace_id, MediaObjectRecord.lifecycle_state == "active", MediaObjectRecord.deleted_at.is_(None))))
        errors: list[str] = []
        records = [
            *session.scalars(select(ProjectRecord).where(ProjectRecord.user_id == user_id, ProjectRecord.workspace_id == workspace_id, ProjectRecord.deleted_at.is_(None))),
            *session.scalars(select(SeriesRecord).where(SeriesRecord.user_id == user_id, SeriesRecord.workspace_id == workspace_id, SeriesRecord.deleted_at.is_(None))),
        ]
        for record in records:
            if isinstance(record, ProjectRecord) and record.series_id and record.series_id not in series_ids:
                errors.append(f"项目 {record.id} 的系列关联无效")
            for reference in _payload_media_references(record.payload):
                if reference.startswith("media:"):
                    try:
                        media_id = parse_database_id(
                            reference.removeprefix("media:"),
                            field="媒体 ID",
                        )
                    except ValueError:
                        media_id = None
                    if media_id not in media_ids:
                        errors.append(f"内容 {record.id} 的媒体关联无效")
        return errors

    @staticmethod
    def _payload_references_media(payload: Any, media_id: str) -> bool:
        return any(reference == f"media:{media_id}" for reference in _payload_media_references(payload))

    def _rollback_item(self, session, batch: ImportBatchRecord, item: ImportBatchItemRecord, now: datetime, expires_at: datetime) -> bool:
        if not item.target_id:
            return False
        try:
            target_database_id = parse_database_id(item.target_id, field="目标 ID")
        except ValueError:
            return False
        if item.item_type == "project":
            record = session.get(ProjectRecord, target_database_id)
            batch_asset_ids = set(
                filter(
                    None,
                    session.scalars(
                        select(ImportBatchItemRecord.target_id).where(
                            ImportBatchItemRecord.batch_id == batch.id,
                            ImportBatchItemRecord.item_type == "asset",
                            ImportBatchItemRecord.created_by_batch.is_(True),
                        )
                    ),
                )
            )
            active_assets = list(
                session.scalars(
                    select(AssetRecord.id).where(
                        AssetRecord.project_id == target_database_id,
                        AssetRecord.deleted_at.is_(None),
                    )
                )
            )
            has_nonbatch_asset = any(str(asset_id) not in batch_asset_ids for asset_id in active_assets)
            has_task = bool(
                session.scalar(
                    select(func.count(AITaskRecord.id)).where(
                        AITaskRecord.project_id == target_database_id
                    )
                )
            )
            if (
                record is None
                or record.version != 1
                or record.deleted_at is not None
                or has_nonbatch_asset
                or has_task
            ):
                return False
            record.deleted_at = now
            record.retention_expires_at = expires_at
            record.version += 1
            return True
        if item.item_type == "series":
            record = session.get(SeriesRecord, target_database_id)
            active_projects = session.scalar(select(func.count(ProjectRecord.id)).where(ProjectRecord.series_id == target_database_id, ProjectRecord.deleted_at.is_(None)))
            batch_asset_ids = set(
                filter(
                    None,
                    session.scalars(
                        select(ImportBatchItemRecord.target_id).where(
                            ImportBatchItemRecord.batch_id == batch.id,
                            ImportBatchItemRecord.item_type == "asset",
                            ImportBatchItemRecord.created_by_batch.is_(True),
                        )
                    ),
                )
            )
            active_assets = list(
                session.scalars(
                    select(AssetRecord.id).where(
                        AssetRecord.series_id == target_database_id,
                        AssetRecord.deleted_at.is_(None),
                    )
                )
            )
            has_nonbatch_asset = any(str(asset_id) not in batch_asset_ids for asset_id in active_assets)
            if (
                record is None
                or record.version != 1
                or record.deleted_at is not None
                or active_projects
                or has_nonbatch_asset
            ):
                return False
            record.deleted_at = now
            record.retention_expires_at = expires_at
            record.version += 1
            return True
        if item.item_type == "asset":
            record = session.get(AssetRecord, target_database_id)
            if record is None or record.version != 1 or record.deleted_at is not None or str(record.provenance.get("import_batch_id")) != str(batch.id):
                return False
            if record.project_id is not None:
                project = session.get(ProjectRecord, record.project_id)
                if project is not None and project.deleted_at is None and project.version > 1:
                    return False
            if record.series_id is not None:
                series = session.get(SeriesRecord, record.series_id)
                if series is not None and series.deleted_at is None and series.version > 1:
                    return False
            record.deleted_at = now
            record.retention_expires_at = expires_at
            record.version += 1
            return True
        if item.item_type == "playground_history":
            record = session.get(ImportedPlaygroundHistoryRecord, target_database_id)
            if record is None or record.deleted_at is not None or record.import_batch_id != batch.id:
                return False
            record.deleted_at = now
            record.retention_expires_at = expires_at
            return True
        if item.item_type == "media":
            record = session.get(MediaObjectRecord, target_database_id)
            if record is None or record.lifecycle_state != "active" or str(record.provenance.get("import_batch_id")) != str(batch.id):
                return False
            asset_reference = session.scalar(select(func.count(AssetRecord.id)).where(AssetRecord.media_object_id == target_database_id, AssetRecord.deleted_at.is_(None)))
            content_records = [
                *session.scalars(select(ProjectRecord).where(ProjectRecord.user_id == batch.target_user_id, ProjectRecord.workspace_id == batch.target_workspace_id, ProjectRecord.deleted_at.is_(None))),
                *session.scalars(select(SeriesRecord).where(SeriesRecord.user_id == batch.target_user_id, SeriesRecord.workspace_id == batch.target_workspace_id, SeriesRecord.deleted_at.is_(None))),
                *session.scalars(select(ImportedPlaygroundHistoryRecord).where(ImportedPlaygroundHistoryRecord.user_id == batch.target_user_id, ImportedPlaygroundHistoryRecord.workspace_id == batch.target_workspace_id, ImportedPlaygroundHistoryRecord.deleted_at.is_(None))),
            ]
            if asset_reference or any(self._payload_references_media(record.payload, item.target_id) for record in content_records):
                return False
            record.lifecycle_state = "deleted"
            record.deleted_at = now
            record.retention_expires_at = expires_at
            return True
        return False
