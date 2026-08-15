from __future__ import annotations

import base64
import hashlib
import hmac
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .ai_io import DownloadedProviderOutput, ProviderOutputReference
from .ai_worker import ProviderInvocationOutcome
from .settings import (
    DeploymentMode,
    DeploymentSettings,
    ObjectStoreAdapter,
    ProviderAdapter,
)


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)
_MP4_STUB = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"
_WAV_STUB = (
    b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


def require_test_adapters(settings: DeploymentSettings) -> None:
    if (
        settings.deployment_mode is not DeploymentMode.TEST
        or not settings.test_adapters_enabled
        or settings.object_store_adapter is not ObjectStoreAdapter.DETERMINISTIC
        or settings.provider_adapter is not ProviderAdapter.DETERMINISTIC
        or settings.test_signing_secret is None
    ):
        raise RuntimeError("确定性适配器只能在显式发布测试模式中启用")


class DeterministicPrivateObjectStore:
    def __init__(self, settings: DeploymentSettings) -> None:
        require_test_adapters(settings)
        self.root = settings.test_object_store_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.signing_secret = settings.test_signing_secret.get_secret_value().encode()

    def _path(self, object_key: str) -> Path:
        candidate = (self.root / object_key).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("测试对象键越界")
        return candidate

    def _signature(self, object_key: str, expires_at: int) -> str:
        message = f"{expires_at}\n{object_key}".encode()
        return hmac.new(self.signing_secret, message, hashlib.sha256).hexdigest()

    def put(self, object_key: str, content: bytes, content_type: str) -> None:
        del content_type
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def signed_get_url(self, object_key: str, expires_seconds: int) -> str:
        expires_at = int(time.time()) + expires_seconds
        signature = self._signature(object_key, expires_at)
        encoded_key = quote(object_key, safe="/")
        return (
            f"/api/v1/_release-test/objects/{encoded_key}"
            f"?expires={expires_at}&signature={signature}"
        )

    def verify(self, object_key: str, expires_at: int, signature: str) -> Path:
        if expires_at < int(time.time()):
            raise ValueError("测试对象授权已过期")
        expected = self._signature(object_key, expires_at)
        if not hmac.compare_digest(expected, signature):
            raise ValueError("测试对象签名无效")
        path = self._path(object_key)
        if not path.is_file():
            raise FileNotFoundError(object_key)
        return path

    def delete(self, object_key: str) -> None:
        self._path(object_key).unlink(missing_ok=True)


class DeterministicModelClientFactory:
    def __init__(self, settings: DeploymentSettings) -> None:
        require_test_adapters(settings)

    def create(self, snapshot):
        if snapshot.provider != "deterministic":
            raise RuntimeError("发布测试任务只能使用 deterministic provider")
        return SimpleNamespace(snapshot=snapshot, adapter="deterministic")


class DeterministicProviderInvoker:
    def __init__(self, settings: DeploymentSettings) -> None:
        require_test_adapters(settings)

    def invoke(self, client, task, on_provider_submission) -> ProviderInvocationOutcome:
        if getattr(client, "adapter", None) != "deterministic":
            raise RuntimeError("确定性供应商客户端无效")
        on_provider_submission(
            "deterministic",
            f"test-task-{task.task_id}",
            f"test-request-{task.attempt_id}",
        )
        parameters = dict(task.model_route.parameters)
        capability = task.capability
        if capability.startswith("image."):
            output_count = int(parameters.get("output_count", parameters.get("count", 1)))
            outputs = [
                {
                    "url": f"lumenx-test://{task.task_id}/output-{index}.png",
                    "filename": f"output-{index}.png",
                    "content_type": "image/png",
                }
                for index in range(output_count)
            ]
            return ProviderInvocationOutcome(
                raw_usage={
                    "output_count": output_count,
                    "resolution": parameters.get("resolution", parameters.get("size")),
                },
                result={"outputs": outputs},
            )
        if capability.startswith("video."):
            output_count = int(parameters.get("output_count", 1))
            outputs = [
                {
                    "url": f"lumenx-test://{task.task_id}/output-{index}.mp4",
                    "filename": f"output-{index}.mp4",
                    "content_type": "video/mp4",
                }
                for index in range(output_count)
            ]
            return ProviderInvocationOutcome(
                raw_usage={
                    "duration_seconds": int(parameters.get("duration", 1)),
                    "output_count": output_count,
                    "resolution": parameters.get("resolution", parameters.get("size")),
                    "audio": bool(parameters.get("generate_audio", False)),
                },
                result={"outputs": outputs},
            )
        if capability.startswith(("speech.", "audio.")):
            return ProviderInvocationOutcome(
                raw_usage={"units": 1},
                result={
                    "outputs": [
                        {
                            "url": f"lumenx-test://{task.task_id}/output.wav",
                            "filename": "output.wav",
                            "content_type": "audio/wav",
                        }
                    ]
                },
            )
        content = task.request_payload.get("content") or "确定性发布测试结果"
        return ProviderInvocationOutcome(
            raw_usage={"input_tokens": 8, "output_tokens": 8},
            result={"content": str(content)},
        )


class DeterministicProviderOutputDownloader:
    def __init__(self, settings: DeploymentSettings) -> None:
        require_test_adapters(settings)

    def download(
        self,
        provider: str,
        reference: ProviderOutputReference,
    ) -> DownloadedProviderOutput:
        if provider != "deterministic" or not reference.url.startswith("lumenx-test://"):
            raise RuntimeError("发布测试输出引用无效")
        content_type = reference.declared_content_type or "application/octet-stream"
        content = {
            "image/png": _PNG_1X1,
            "video/mp4": _MP4_STUB,
            "audio/wav": _WAV_STUB,
        }.get(content_type)
        if content is None:
            raise RuntimeError("发布测试输出类型不受支持")
        return DownloadedProviderOutput(
            content=content,
            content_type=content_type,
            filename=reference.filename,
        )


def install_test_object_api(
    app: FastAPI,
    settings: DeploymentSettings,
    store: DeterministicPrivateObjectStore,
) -> None:
    require_test_adapters(settings)

    @app.get("/_release-test/objects/{object_key:path}", include_in_schema=False)
    def read_test_object(
        object_key: str,
        expires: int = Query(),
        signature: str = Query(min_length=64, max_length=64),
    ) -> FileResponse:
        try:
            path = store.verify(object_key, expires, signature)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="测试对象不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return FileResponse(path)
