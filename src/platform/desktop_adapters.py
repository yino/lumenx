from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote

from pydantic import SecretStr

from src.utils.model_catalog import (
    GENERATED_MODEL_CATALOG_PATH,
    load_generated_model_catalog,
)

from .contracts import (
    MediaWrite,
    StoredMedia,
    TicketReservation,
    UsageSettlement,
    UserContext,
    WorkspaceContext,
    ModelRouteSnapshot,
)
from .credentials import CredentialResolutionError, SECRET_REFERENCE_PATTERN


class LocalIdentityContextProvider:
    def __init__(self, user_id: str = "desktop-local-user") -> None:
        if not user_id.strip():
            raise ValueError("桌面本地用户标识不能为空")
        self._identity = UserContext(user_id=user_id.strip(), session_id="desktop-local")

    def current_user(self) -> UserContext:
        return self._identity


class DesktopCredentialProvider:
    """Resolve provider credentials configured by the local desktop user."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = environment if environment is not None else os.environ

    def resolve(self, secret_ref: str) -> SecretStr:
        if not SECRET_REFERENCE_PATTERN.fullmatch(secret_ref):
            raise CredentialResolutionError("本地凭据名称格式无效")
        value = (self.environment.get(secret_ref) or "").strip()
        if not value:
            raise CredentialResolutionError("请先在桌面设置中配置模型凭据")
        lowered = value.lower()
        if lowered.startswith(("your_", "change-me", "replace-with")):
            raise CredentialResolutionError("本地模型凭据仍是占位值")
        return SecretStr(value)


class DesktopCatalogModelConfigurationProvider:
    """Select local user-funded routes from the bundled desktop model catalog."""

    _CAPABILITY_DEFAULTS = {
        "image.t2i": "t2i_model",
        "image.i2i": "i2i_model",
        "video.i2v": "i2v_model",
        "video.r2v": "r2v_model",
    }
    _DIRECT_ROUTES = {
        "script.analysis": ("dashscope", "qwen3.6-plus", "DASHSCOPE_API_KEY"),
        "prompt.polish": ("dashscope", "qwen3.6-plus", "DASHSCOPE_API_KEY"),
        "speech.tts": ("dashscope", "qwen3-tts-instruct", "DASHSCOPE_API_KEY"),
    }

    def __init__(self, catalog_path: str | os.PathLike[str] = GENERATED_MODEL_CATALOG_PATH) -> None:
        self.catalog_path = Path(catalog_path)
        self.catalog = load_generated_model_catalog(self.catalog_path)
        self.version = str(self.catalog.get("version", "unknown"))

    def select_route(
        self,
        capability: str,
        requested_parameters: Mapping[str, object],
    ) -> ModelRouteSnapshot:
        defaults = self.catalog.get("defaults", {}).get("model_settings", {})
        model_key = self._CAPABILITY_DEFAULTS.get(capability)
        if model_key is not None:
            model_id = str(defaults.get(model_key) or "")
            model = self.catalog.get("models", {}).get(model_id)
            if not model_id or not isinstance(model, dict):
                raise LookupError(f"桌面模型目录缺少能力 {capability} 的默认模型")
            provider = str(model.get("default_backend") or model.get("provider") or "")
            references = model.get("credential_sources", {}).get(provider, [])
            secret_ref = str(references[0]) if references else "DASHSCOPE_API_KEY"
            display_name = str(model.get("display_name") or model_id)
        else:
            direct = self._DIRECT_ROUTES.get(capability)
            if direct is None:
                raise LookupError(f"桌面模型目录不支持能力 {capability}")
            provider, model_id, secret_ref = direct
            display_name = model_id

        route_key = f"{capability}:{provider}:{model_id}"
        route_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lumenx:desktop:{route_key}"))
        parameters = json.loads(
            json.dumps(dict(requested_parameters), ensure_ascii=False, allow_nan=False)
        )
        return ModelRouteSnapshot(
            config_version_id=f"desktop-catalog-{self.version}",
            route_id=route_id,
            capability=capability,
            provider=provider,
            provider_model_id=model_id,
            display_name=display_name,
            parameters=parameters,
            metering_formula={"kind": "desktop_no_charge"},
            fallback_policy={"enabled": False},
            secret_ref=secret_ref,
        )


class DesktopMediaStorage:
    """Store desktop media below a local output root and expose ``/files`` URLs."""

    def __init__(
        self,
        root: str | os.PathLike[str] = "output",
        public_prefix: str = "/files",
    ) -> None:
        self.root = Path(root).resolve()
        self.public_prefix = "/" + public_prefix.strip("/")
        self._records: dict[str, StoredMedia] = {}
        self._lock = threading.RLock()

    def store(self, context: WorkspaceContext, media: MediaWrite) -> StoredMedia:
        del context
        if not media.content:
            raise ValueError("桌面媒体内容不能为空")
        if not re.fullmatch(r"[\w.+-]+/[\w.+-]+", media.content_type):
            raise ValueError("桌面媒体类型无效")
        filename = Path(media.filename).name
        if not filename or filename in {".", ".."}:
            raise ValueError("桌面媒体文件名无效")
        suffix = Path(filename).suffix.lower()
        if not suffix:
            suffix = mimetypes.guess_extension(media.content_type) or ".bin"
        media_id = str(uuid.uuid4())
        scope = "shared" if media.project_id is None else self._safe_segment(media.project_id)
        relative_path = Path("media") / scope / f"{media_id}{suffix}"
        destination = (self.root / relative_path).resolve()
        if self.root not in destination.parents:
            raise ValueError("桌面媒体路径越界")
        checksum = hashlib.sha256(media.content).hexdigest()
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            try:
                temporary.write_bytes(media.content)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            stored = StoredMedia(
                media_id=media_id,
                object_key=relative_path.as_posix(),
                content_type=media.content_type,
                size_bytes=len(media.content),
                checksum_sha256=checksum,
            )
            self._records[media_id] = stored
            return stored

    def authorized_url(
        self,
        context: WorkspaceContext,
        media_id: str,
        expires_at: datetime,
    ) -> str:
        del context, expires_at
        with self._lock:
            stored = self._records.get(media_id)
            if stored is None:
                raise FileNotFoundError("桌面媒体不存在")
            path = (self.root / stored.object_key).resolve()
            if not path.is_file():
                raise FileNotFoundError("桌面媒体文件不存在")
            return f"{self.public_prefix}/{quote(stored.object_key, safe='/')}"

    def delete(self, context: WorkspaceContext, media_id: str) -> None:
        del context
        with self._lock:
            stored = self._records.pop(media_id, None)
            if stored is None:
                raise FileNotFoundError("桌面媒体不存在")
            path = (self.root / stored.object_key).resolve()
            if self.root not in path.parents:
                raise ValueError("桌面媒体路径越界")
            path.unlink(missing_ok=True)

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("桌面项目标识包含非法字符")
        return value


class InProcessTaskDispatcher:
    def __init__(
        self,
        handler: Callable[[str], None],
        *,
        executor: Executor | None = None,
        max_workers: int = 4,
    ) -> None:
        if max_workers < 1:
            raise ValueError("桌面任务并发数必须为正整数")
        self.handler = handler
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="lumenx-desktop-task",
        )
        self._owns_executor = executor is None

    def dispatch(self, task_id: str) -> None:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("桌面任务标识无效")
        self._executor.submit(self.handler, task_id.strip())

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=True)


class NoOpDesktopBillingService:
    """Desktop runs on user-owned credentials, so platform ticket billing is disabled."""

    def reserve(
        self,
        context: WorkspaceContext,
        task_id: str,
        maximum_metering_tokens: int,
        tokens_per_ticket: int,
    ) -> TicketReservation:
        del context, maximum_metering_tokens
        if not task_id:
            raise ValueError("桌面任务标识不能为空")
        if tokens_per_ticket <= 0:
            raise ValueError("算力券换算比例必须为正整数")
        return TicketReservation(
            hold_id=f"desktop-no-charge:{task_id}",
            quoted_microtickets=0,
            tokens_per_ticket=tokens_per_ticket,
        )

    def settle(self, context: WorkspaceContext, settlement: UsageSettlement) -> None:
        del context, settlement

    def release(self, context: WorkspaceContext, task_id: str, reason: str) -> None:
        del context, task_id, reason
