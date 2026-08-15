from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai_request_policy import ClientAIOverrideError, enforce_no_client_ai_overrides
from .asset_repositories import PostgresWorkspaceAssetRepository, StoredAsset
from .asset_service import CloudAssetService
from .content_repositories import (
    InvalidRepositoryContextError,
    OptimisticVersionConflictError,
    ScopedDocumentNotFoundError,
)
from .contracts import WorkspaceContext
from .database import Database
from .db_models import AITaskRecord, WorkspaceRecord
from .media_storage import PostgresMediaRepository
from .identifiers import parse_database_id
from .ticket_math import MICROTICKETS_PER_TICKET


class CloudPlaygroundValidationError(ValueError):
    pass


def _ticket_text(microtickets: int) -> str:
    whole, fraction = divmod(abs(microtickets), MICROTICKETS_PER_TICKET)
    sign = "-" if microtickets < 0 else ""
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


class CloudPlaygroundService:
    TEMPLATE_ORIGIN = "playground_template"
    TASK_CAPABILITY_PREFIX = "playground."

    def __init__(self, database: Database) -> None:
        self.database = database
        self.templates = PostgresWorkspaceAssetRepository(database)
        self.assets = CloudAssetService(database)
        self.media = PostgresMediaRepository(database)

    @staticmethod
    def _context_ids(context: WorkspaceContext) -> tuple[int, int]:
        if not isinstance(context, WorkspaceContext):
            raise InvalidRepositoryContextError(
                "Playground 服务必须包含用户和工作区上下文"
            )
        try:
            return (
                parse_database_id(context.identity.user_id, field="用户 ID"),
                parse_database_id(context.workspace_id, field="工作区 ID"),
            )
        except ValueError as exc:
            raise InvalidRepositoryContextError("用户或工作区标识无效") from exc

    @staticmethod
    def _require_workspace(
        session: Session,
        user_id: int,
        workspace_id: int,
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

    @classmethod
    def _is_template(cls, asset: StoredAsset) -> bool:
        return (
            asset.asset_type == "other"
            and asset.provenance.get("origin") == cls.TEMPLATE_ORIGIN
            and isinstance(asset.document, dict)
            and asset.document.get("kind") == cls.TEMPLATE_ORIGIN
        )

    @staticmethod
    def _template_response(asset: StoredAsset) -> dict[str, Any]:
        if not isinstance(asset.document, dict):
            raise CloudPlaygroundValidationError("提示词模板数据无效")
        payload = copy.deepcopy(asset.document)
        payload["version"] = asset.version
        return payload

    @staticmethod
    def _validate_template_parameters(parameters: Mapping[str, Any]) -> None:
        try:
            enforce_no_client_ai_overrides(parameters)
        except ClientAIOverrideError as exc:
            raise CloudPlaygroundValidationError(str(exc)) from exc

    def list_templates(self, context: WorkspaceContext) -> list[dict[str, Any]]:
        return [
            self._template_response(asset)
            for asset in self.templates.list(context, asset_type="other")
            if self._is_template(asset)
        ]

    def _require_template(
        self,
        context: WorkspaceContext,
        template_id: str,
    ) -> StoredAsset:
        asset = self.templates.find_by_domain_id(
            context,
            None,
            "other",
            template_id,
        )
        if asset is None or not self._is_template(asset):
            raise ScopedDocumentNotFoundError("资源不存在")
        return asset

    def create_template(
        self,
        context: WorkspaceContext,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._validate_template_parameters(payload.get("default_parameters") or {})
        now = datetime.now(UTC).isoformat()
        document = {
            "id": str(uuid.uuid4()),
            "kind": self.TEMPLATE_ORIGIN,
            "name": str(payload["name"]).strip(),
            "category": payload.get("category") or "general",
            "prompt": payload["prompt"],
            "negative_prompt": payload.get("negative_prompt"),
            "default_mode": payload.get("default_mode"),
            "default_parameters": dict(payload.get("default_parameters") or {}),
            "created_at": now,
            "updated_at": now,
        }
        stored = self.templates.add(
            context,
            None,
            "other",
            document,
            provenance={"origin": self.TEMPLATE_ORIGIN},
        )
        return self._template_response(stored)

    def update_template(
        self,
        context: WorkspaceContext,
        template_id: str,
        updates: Mapping[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        if "default_parameters" in updates:
            self._validate_template_parameters(
                updates.get("default_parameters") or {}
            )
        stored = self._require_template(context, template_id)
        if stored.version != expected_version:
            raise OptimisticVersionConflictError(
                "提示词模板已在其他位置更新，请刷新后重试"
            )
        if not isinstance(stored.document, dict):
            raise CloudPlaygroundValidationError("提示词模板数据无效")
        document = copy.deepcopy(stored.document)
        document.update(dict(updates))
        document["updated_at"] = datetime.now(UTC).isoformat()
        updated = self.templates.update(
            context,
            None,
            stored.record_id,
            document,
            expected_version,
        )
        return self._template_response(updated)

    def delete_template(
        self,
        context: WorkspaceContext,
        template_id: str,
        expected_version: int,
    ) -> None:
        stored = self._require_template(context, template_id)
        self.templates.soft_delete(
            context,
            None,
            stored.record_id,
            expected_version,
        )

    def _task_statement(
        self,
        user_id: int,
        workspace_id: int,
    ):
        return select(AITaskRecord).where(
            AITaskRecord.user_id == user_id,
            AITaskRecord.workspace_id == workspace_id,
        )

    @classmethod
    def _is_playground_task(cls, task: AITaskRecord) -> bool:
        if task.capability.startswith(cls.TASK_CAPABILITY_PREFIX):
            return True
        payload = task.request_payload if isinstance(task.request_payload, dict) else {}
        content = payload.get("content")
        return (
            isinstance(content, Mapping)
            and str(content.get("operation", "")).startswith("playground.")
        )

    def _require_task(
        self,
        session: Session,
        context: WorkspaceContext,
        generation_id: str,
    ) -> AITaskRecord:
        user_id, workspace_id = self._context_ids(context)
        try:
            task_id = parse_database_id(generation_id, field="任务 ID")
        except ValueError as exc:
            raise ScopedDocumentNotFoundError("资源不存在") from exc
        task = session.scalar(
            self._task_statement(user_id, workspace_id).where(
                AITaskRecord.id == task_id
            )
        )
        if (
            task is None
            or not self._is_playground_task(task)
            or self._is_hidden(task.result)
        ):
            raise ScopedDocumentNotFoundError("资源不存在")
        return task

    @staticmethod
    def _is_hidden(result: Mapping[str, Any] | None) -> bool:
        return bool(
            isinstance(result, Mapping)
            and isinstance(result.get("playground"), Mapping)
            and result["playground"].get("hidden_at")
        )

    def _outputs(
        self,
        context: WorkspaceContext,
        task: AITaskRecord,
        session: Session,
    ) -> list[dict[str, Any]]:
        result = task.result if isinstance(task.result, dict) else {}
        raw_outputs = result.get("outputs", [])
        if not isinstance(raw_outputs, list):
            raw_outputs = []
        if not raw_outputs and isinstance(result.get("media_ids"), list):
            raw_outputs = [
                {"id": f"media-{index + 1}", "media_id": media_id}
                for index, media_id in enumerate(result["media_ids"])
            ]
        playground_meta = result.get("playground")
        saved_outputs = (
            playground_meta.get("saved_output_assets", {})
            if isinstance(playground_meta, Mapping)
            else {}
        )
        outputs: list[dict[str, Any]] = []
        for item in raw_outputs:
            if not isinstance(item, dict):
                continue
            media_id = str(item.get("media_id", "")).strip()
            media = self.media.get(
                context,
                media_id,
                session=session,
            ) if media_id else None
            if media is None:
                continue
            output_id = str(item.get("id") or media_id)
            media_type = item.get("media_type")
            if not media_type:
                media_type = "video" if media.mime_type.startswith("video/") else "image"
            outputs.append(
                {
                    "id": output_id,
                    "media_id": media_id,
                    "media_type": media_type,
                    "saved_asset_id": item.get("saved_asset_id")
                    or saved_outputs.get(output_id),
                }
            )
        return outputs

    def _task_response(
        self,
        context: WorkspaceContext,
        task: AITaskRecord,
        session: Session,
    ) -> dict[str, Any]:
        request_payload = (
            task.request_payload if isinstance(task.request_payload, dict) else {}
        )
        content = request_payload.get("content")
        content_payload = content if isinstance(content, Mapping) else request_payload
        config_snapshot = (
            task.config_snapshot if isinstance(task.config_snapshot, dict) else {}
        )
        return {
            "id": str(task.id),
            "mode": content_payload.get("mode"),
            "prompt": content_payload.get("prompt", ""),
            "negative_prompt": content_payload.get("negative_prompt"),
            "input_media_ids": list(request_payload.get("input_media_ids") or []),
            "parameters": dict(request_payload.get("parameters") or {}),
            "batch_size": content_payload.get("batch_size", 1),
            "outputs": self._outputs(context, task, session),
            "status": task.status,
            "raw_status": task.status,
            "status_zh": {
                "reserved": "预扣中",
                "queued": "排队中",
                "running": "生成中",
                "provider_succeeded": "结果处理中",
                "succeeded": "已完成",
                "failed": "已失败",
                "cancelled": "已取消",
                "support_review": "计费待复核",
            }.get(task.status, task.status),
            "quoted_microtickets": str(task.quoted_microtickets),
            "quoted_tickets": _ticket_text(task.quoted_microtickets),
            "tokens_per_ticket": str(task.tokens_per_ticket),
            "cancellation_requested": task.cancellation_requested_at is not None,
            "support_review": task.status == "support_review",
            "support_review_reason": task.support_review_reason,
            "error_code": task.safe_error_code,
            "error": task.safe_error_message,
            "actual_model_name": config_snapshot.get("display_name"),
            "actual_model_id": config_snapshot.get("provider_model_id"),
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        }

    def list_history(
        self,
        context: WorkspaceContext,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        user_id, workspace_id = self._context_ids(context)
        with self.database.transaction(context.identity) as session:
            self._require_workspace(session, user_id, workspace_id)
            tasks = list(
                session.scalars(
                    self._task_statement(user_id, workspace_id).order_by(
                        AITaskRecord.created_at.desc(),
                        AITaskRecord.id.desc(),
                    )
                )
            )
            visible = [
                task
                for task in tasks
                if self._is_playground_task(task) and not self._is_hidden(task.result)
            ]
            return [
                self._task_response(context, task, session)
                for task in visible[offset : offset + limit]
            ]

    def get_generation(
        self,
        context: WorkspaceContext,
        generation_id: str,
    ) -> dict[str, Any]:
        with self.database.transaction(context.identity) as session:
            task = self._require_task(session, context, generation_id)
            return self._task_response(context, task, session)

    def hide_generation(
        self,
        context: WorkspaceContext,
        generation_id: str,
    ) -> None:
        with self.database.transaction(context.identity) as session:
            task = self._require_task(session, context, generation_id)
            result = copy.deepcopy(task.result or {})
            playground = dict(result.get("playground") or {})
            playground["hidden_at"] = datetime.now(UTC).isoformat()
            result["playground"] = playground
            task.result = result

    def save_output_to_library(
        self,
        context: WorkspaceContext,
        generation_id: str,
        output_id: str,
        category: str,
    ) -> StoredAsset:
        with self.database.transaction(context.identity) as session:
            task = self._require_task(session, context, generation_id)
            output = next(
                (
                    item
                    for item in self._outputs(context, task, session)
                    if str(item.get("id")) == output_id
                ),
                None,
            )
            if output is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            media_id = str(output.get("media_id", ""))
            media = self.media.require(context, media_id, session=session)
            if media.project_id is not None:
                raise ScopedDocumentNotFoundError("资源不存在")
            request_payload = (
                task.request_payload if isinstance(task.request_payload, dict) else {}
            )
            content = request_payload.get("content")
            content_payload = content if isinstance(content, Mapping) else request_payload
            prompt = str(content_payload.get("prompt", "")).strip()

        asset = self.assets.create_library_asset(
            context,
            category,
            media_id=media_id,
            name=prompt[:40] or "Playground 创作结果",
            description=prompt,
        )

        with self.database.transaction(context.identity) as session:
            task = self._require_task(session, context, generation_id)
            result = copy.deepcopy(task.result or {})
            outputs = result.get("outputs")
            saved_in_output = False
            if isinstance(outputs, list):
                for item in outputs:
                    if isinstance(item, dict) and str(item.get("id")) == output_id:
                        item["saved_asset_id"] = asset.domain_id
                        saved_in_output = True
                        break
            if not saved_in_output:
                playground = dict(result.get("playground") or {})
                saved_outputs = dict(playground.get("saved_output_assets") or {})
                saved_outputs[output_id] = asset.domain_id
                playground["saved_output_assets"] = saved_outputs
                result["playground"] = playground
            task.result = result
        return asset
