from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any, Protocol
from urllib.parse import urlparse

import requests
from sqlalchemy import and_, or_, select

from .ai_task_state import AITaskStateService
from .content_repositories import ScopedDocumentNotFoundError
from .contracts import MediaWrite, WorkspaceContext
from .db_models import AssetRecord
from .media_storage import CloudMediaStorage
from .identifiers import parse_database_id
from .ticket_settlement import TicketSettlementService


class AIProviderIOError(RuntimeError):
    pass


class ProviderInputNotFoundError(AIProviderIOError):
    pass


class ProviderOutputValidationError(AIProviderIOError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedProviderInput:
    media_id: str
    content_type: str
    signed_url: str


@dataclass(frozen=True, slots=True)
class ProviderOutputReference:
    url: str
    filename: str
    declared_content_type: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadedProviderOutput:
    content: bytes
    content_type: str
    filename: str


@dataclass(frozen=True, slots=True)
class FinalizedAIResult:
    task_id: str
    status: str
    media_ids: tuple[str, ...]
    charged_microtickets: int
    support_review: bool = False
    reused: bool = False


class ProviderOutputDownloader(Protocol):
    def download(
        self,
        provider: str,
        reference: ProviderOutputReference,
    ) -> DownloadedProviderOutput: ...


class AIResultApplier(Protocol):
    def apply(
        self,
        context: WorkspaceContext,
        task: Any,
        media_ids: Sequence[str],
        content: str | Mapping[str, Any] | None,
    ) -> str | dict[str, Any] | None: ...


class AIProviderInputResolver:
    def __init__(
        self,
        storage: CloudMediaStorage,
        *,
        signed_url_seconds: int = 300,
    ) -> None:
        if signed_url_seconds < 30 or signed_url_seconds > 900:
            raise ValueError("供应商输入链接有效期必须在 30 到 900 秒之间")
        self.storage = storage
        self.signed_url_seconds = signed_url_seconds

    def resolve(
        self,
        context: WorkspaceContext,
        *,
        request_payload: Mapping[str, Any],
        project_id: str | None,
    ) -> tuple[ResolvedProviderInput, ...]:
        media_ids = list(request_payload.get("input_media_ids") or [])
        resource_ids = request_payload.get("resource_ids") or {}
        if not isinstance(resource_ids, Mapping):
            raise ProviderInputNotFoundError("AI 输入资源不存在")
        asset_ids = []
        for kind in ("asset", "voice", "template"):
            values = resource_ids.get(kind) or []
            if not isinstance(values, list):
                raise ProviderInputNotFoundError("AI 输入资源不存在")
            asset_ids.extend(values)

        if asset_ids:
            try:
                canonical_asset_ids = [
                    parse_database_id(value, field="资产 ID") for value in asset_ids
                ]
                user_id = parse_database_id(context.identity.user_id, field="用户 ID")
                workspace_id = parse_database_id(context.workspace_id, field="工作区 ID")
            except ValueError as exc:
                raise ProviderInputNotFoundError("AI 输入资源不存在") from exc
            with self.storage.database.transaction(context.identity) as session:
                records = list(
                    session.scalars(
                        select(AssetRecord).where(
                            AssetRecord.id.in_(canonical_asset_ids),
                            AssetRecord.deleted_at.is_(None),
                            or_(
                                AssetRecord.scope == "system",
                                and_(
                                    AssetRecord.user_id == user_id,
                                    AssetRecord.workspace_id == workspace_id,
                                ),
                            ),
                        )
                    )
                )
                if {record.id for record in records} != set(canonical_asset_ids):
                    raise ProviderInputNotFoundError("AI 输入资源不存在")
                media_ids.extend(
                    str(record.media_object_id)
                    for record in records
                    if record.scope != "system" and record.media_object_id is not None
                )

        unique_media_ids = tuple(dict.fromkeys(str(value) for value in media_ids))
        resolved = []
        expires_at = datetime.now(UTC) + timedelta(seconds=self.signed_url_seconds)
        for media_id in unique_media_ids:
            try:
                record = self.storage.repository.require(context, media_id)
            except ScopedDocumentNotFoundError as exc:
                raise ProviderInputNotFoundError("AI 输入资源不存在") from exc
            if project_id is not None and record.project_id not in {
                None,
                parse_database_id(project_id, field="项目 ID"),
            }:
                raise ProviderInputNotFoundError("AI 输入资源不存在")
            try:
                signed_url = self.storage.authorized_url(
                    context,
                    str(record.id),
                    expires_at,
                )
            except ScopedDocumentNotFoundError as exc:
                raise ProviderInputNotFoundError("AI 输入资源不存在") from exc
            resolved.append(
                ResolvedProviderInput(
                    media_id=str(record.id),
                    content_type=record.mime_type,
                    signed_url=signed_url,
                )
            )
        return tuple(resolved)


class HTTPProviderOutputDownloader:
    def __init__(
        self,
        allowed_host_suffixes: Mapping[str, Sequence[str]],
        *,
        maximum_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        if maximum_bytes <= 0:
            raise ValueError("供应商输出大小上限必须为正整数")
        self.allowed_host_suffixes = {
            provider: tuple(
                suffix.lower().lstrip(".")
                for suffix in suffixes
                if suffix.strip()
            )
            for provider, suffixes in allowed_host_suffixes.items()
        }
        self.maximum_bytes = maximum_bytes

    def _validate_url(self, provider: str, url: str) -> None:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
            raise ProviderOutputValidationError("供应商输出地址无效")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ProviderOutputValidationError("供应商输出地址无效")
        suffixes = self.allowed_host_suffixes.get(provider, ())
        if not suffixes or not any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in suffixes
        ):
            raise ProviderOutputValidationError("供应商输出地址不在允许域名中")

    def download(
        self,
        provider: str,
        reference: ProviderOutputReference,
    ) -> DownloadedProviderOutput:
        self._validate_url(provider, reference.url)
        try:
            response = requests.get(
                reference.url,
                stream=True,
                timeout=(10, 120),
                allow_redirects=True,
            )
            response.raise_for_status()
            self._validate_url(provider, response.url)
            declared_size = response.headers.get("content-length")
            if declared_size and int(declared_size) > self.maximum_bytes:
                raise ProviderOutputValidationError("供应商输出文件过大")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                content.extend(chunk)
                if len(content) > self.maximum_bytes:
                    raise ProviderOutputValidationError("供应商输出文件过大")
        except ProviderOutputValidationError:
            raise
        except Exception as exc:
            raise ProviderOutputValidationError("供应商输出下载失败") from exc
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        return DownloadedProviderOutput(
            content=bytes(content),
            content_type=content_type,
            filename=reference.filename,
        )


def _output_references(result: Mapping[str, Any]) -> tuple[ProviderOutputReference, ...]:
    raw_outputs = result.get("outputs", [])
    if not isinstance(raw_outputs, list) or len(raw_outputs) > 16:
        raise ProviderOutputValidationError("供应商输出列表无效")
    references = []
    for index, raw in enumerate(raw_outputs):
        if not isinstance(raw, Mapping):
            raise ProviderOutputValidationError("供应商输出引用无效")
        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ProviderOutputValidationError("供应商输出地址无效")
        filename = raw.get("filename") or f"output-{index}"
        if not isinstance(filename, str) or not PurePath(filename).name:
            raise ProviderOutputValidationError("供应商输出文件名无效")
        declared_type = raw.get("content_type")
        if declared_type is not None and not isinstance(declared_type, str):
            raise ProviderOutputValidationError("供应商输出类型无效")
        references.append(
            ProviderOutputReference(
                url=url.strip(),
                filename=PurePath(filename).name,
                declared_content_type=(
                    declared_type.strip().lower() if declared_type else None
                ),
            )
        )
    return tuple(references)


def _validate_output_content(output: DownloadedProviderOutput) -> None:
    content = output.content
    content_type = output.content_type
    if not content:
        raise ProviderOutputValidationError("供应商输出内容为空")
    signatures = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
        "video/mp4": len(content) >= 12 and content[4:8] == b"ftyp",
        "video/webm": content.startswith(b"\x1aE\xdf\xa3"),
        "audio/mpeg": content.startswith(b"ID3")
        or content.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")),
        "audio/wav": content.startswith(b"RIFF") and content[8:12] == b"WAVE",
        "audio/ogg": content.startswith(b"OggS"),
    }
    if content_type not in signatures or not signatures[content_type]:
        raise ProviderOutputValidationError("供应商输出格式校验失败")


def _safe_content(result: Mapping[str, Any]) -> str | dict[str, Any] | None:
    content = result.get("content")
    if content is None:
        return None
    if not isinstance(content, (str, Mapping)):
        raise ProviderOutputValidationError("供应商文本结果无效")
    encoded = json.dumps(content, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise ProviderOutputValidationError("供应商文本结果过大")
    return dict(content) if isinstance(content, Mapping) else content


class AIOutputFinalizationService:
    def __init__(
        self,
        *,
        task_state: AITaskStateService,
        settlement: TicketSettlementService,
        media_storage: CloudMediaStorage,
        downloader: ProviderOutputDownloader,
        result_applier: AIResultApplier | None = None,
    ) -> None:
        self.task_state = task_state
        self.settlement = settlement
        self.media_storage = media_storage
        self.downloader = downloader
        self.result_applier = result_applier

    def finalize(
        self,
        context: WorkspaceContext,
        task_id: str,
    ) -> FinalizedAIResult:
        aggregate = self.task_state.get(context, task_id)
        if aggregate.task.status == "succeeded":
            result = aggregate.task.result or {}
            media_ids = tuple(str(value) for value in result.get("media_ids", []))
            return FinalizedAIResult(
                task_id=task_id,
                status="succeeded",
                media_ids=media_ids,
                charged_microtickets=0,
                reused=True,
            )
        if aggregate.task.status != "provider_succeeded" or not aggregate.attempts:
            raise ProviderOutputValidationError("AI 任务尚未达到结果处理状态")
        attempt = aggregate.attempts[-1]
        media_ids = []
        try:
            raw_result = aggregate.task.result or {}
            references = _output_references(raw_result)
            content = _safe_content(raw_result)
            requires_binary = aggregate.task.capability.startswith(
                ("image.", "video.", "speech.", "audio.")
            )
            if requires_binary and not references:
                raise ProviderOutputValidationError("供应商响应缺少媒体输出")
            if not requires_binary and content is None:
                raise ProviderOutputValidationError("供应商响应缺少文本输出")
            for index, reference in enumerate(references):
                output = self.downloader.download(attempt.provider, reference)
                _validate_output_content(output)
                if (
                    reference.declared_content_type is not None
                    and reference.declared_content_type != output.content_type
                ):
                    raise ProviderOutputValidationError("供应商输出类型不一致")
                stored = self.media_storage.store_generated(
                    context,
                    MediaWrite(
                        content=output.content,
                        content_type=output.content_type,
                        filename=output.filename,
                        project_id=aggregate.task.project_id,
                        provenance={
                            "origin": "ai_generation",
                            "task_id": task_id,
                            "attempt_id": attempt.id,
                            "provider": attempt.provider,
                            "provider_task_id": attempt.provider_task_id,
                            "output_index": index,
                        },
                    ),
                    task_id=task_id,
                    output_index=index,
                )
                media_ids.append(stored.media_id)
            if self.result_applier is not None:
                content = self.result_applier.apply(
                    context,
                    aggregate.task,
                    media_ids,
                    content,
                )
        except Exception:
            failure = self.settlement.settle_billable_failure(
                context,
                task_id=task_id,
                attempt_id=attempt.id,
                raw_provider_usage=attempt.raw_usage or {},
                support_review_reason="供应商已计费，结果下载或保存失败",
                safe_error_code="AI_OUTPUT_PROCESSING_FAILED",
                safe_error_message="AI 结果处理失败，已进入人工复核",
            )
            return FinalizedAIResult(
                task_id=task_id,
                status="support_review",
                media_ids=tuple(media_ids),
                charged_microtickets=failure.charged_microtickets,
                support_review=True,
            )

        safe_result: dict[str, Any] = {
            "media_ids": media_ids,
            "output_count": len(media_ids),
        }
        if content is not None:
            safe_result["content"] = content
        success = self.settlement.settle_success(
            context,
            task_id=task_id,
            attempt_id=attempt.id,
            raw_provider_usage=attempt.raw_usage or {},
            result=safe_result,
        )
        return FinalizedAIResult(
            task_id=task_id,
            status="succeeded",
            media_ids=tuple(media_ids),
            charged_microtickets=success.charged_microtickets,
            reused=success.reused,
        )
