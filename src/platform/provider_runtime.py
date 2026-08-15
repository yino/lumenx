from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .ai_io import DownloadedProviderOutput, ProviderOutputReference
from .ai_worker import ProviderInvocationOutcome
from .settings import DeploymentMode, DeploymentSettings, ProviderAdapter


class ProductionProviderInvoker:
    """Bridges immutable cloud task snapshots to the existing provider adapters."""

    def __init__(self, settings: DeploymentSettings) -> None:
        if (
            settings.deployment_mode is not DeploymentMode.CLOUD
            or settings.provider_adapter is not ProviderAdapter.PRODUCTION
        ):
            raise RuntimeError("生产供应商执行器只能在正常云端模式中启用")
        self.output_root = settings.provider_output_root.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _content(task) -> str:
        content = task.request_payload.get("content")
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    def _output_path(self, task, suffix: str) -> Path:
        path = (self.output_root / task.task_id / f"{task.attempt_id}{suffix}").resolve()
        if not path.is_relative_to(self.output_root):
            raise RuntimeError("供应商临时输出路径越界")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def invoke(self, client, task, on_provider_submission) -> ProviderInvocationOutcome:
        capability = task.capability
        prompt = self._content(task)
        if capability in {"script.analysis", "prompt.polish"}:
            result = client.adapter.chat_with_usage(
                [{"role": "user", "content": prompt}],
                model=task.model_route.provider_model_id,
            )
            on_provider_submission(
                task.model_route.provider,
                None,
                result.provider_request_id,
            )
            return ProviderInvocationOutcome(
                raw_usage=dict(result.raw_usage),
                result={"content": result.content},
            )

        if capability.startswith("image."):
            suffix = ".png"
            content_type = "image/png"
        elif capability.startswith("video."):
            suffix = ".mp4"
            content_type = "video/mp4"
        else:
            raise RuntimeError(f"云端供应商执行器暂不支持能力 {capability}")

        output_path = self._output_path(task, suffix)
        parameters = client.execution_parameters()
        parameters["on_provider_ids"] = on_provider_submission
        input_urls = [item.signed_url for item in task.provider_inputs]
        if capability.startswith("image.") and input_urls:
            parameters["ref_image_paths"] = input_urls
        elif capability.startswith("video.") and input_urls:
            parameters["img_url"] = input_urls[0]
            if capability == "video.r2v":
                parameters["generation_mode"] = "r2v"
                parameters["ref_image_urls"] = input_urls

        generated = client.adapter.generate_with_usage(
            prompt,
            str(output_path),
            **parameters,
        )
        generated_path = Path(generated.output_path).resolve()
        if not generated_path.is_relative_to(self.output_root) or not generated_path.is_file():
            raise RuntimeError("供应商输出未写入受控临时目录")
        if not getattr(on_provider_submission, "recorded", False):
            on_provider_submission(task.model_route.provider, None, None)
        relative = generated_path.relative_to(self.output_root).as_posix()
        return ProviderInvocationOutcome(
            raw_usage=dict(generated.raw_usage),
            result={
                "outputs": [
                    {
                        "url": f"lumenx-worker:///{quote(relative, safe='/')}",
                        "filename": generated_path.name,
                        "content_type": content_type,
                    }
                ]
            },
        )


class ProductionProviderOutputDownloader:
    def __init__(self, settings: DeploymentSettings) -> None:
        if settings.deployment_mode is not DeploymentMode.CLOUD:
            raise RuntimeError("生产输出读取器只能在正常云端模式中启用")
        self.output_root = settings.provider_output_root.resolve()

    def download(
        self,
        provider: str,
        reference: ProviderOutputReference,
    ) -> DownloadedProviderOutput:
        del provider
        parsed = urlparse(reference.url)
        if parsed.scheme != "lumenx-worker" or parsed.netloc:
            raise RuntimeError("供应商临时输出引用无效")
        relative = unquote(parsed.path).lstrip("/")
        path = (self.output_root / relative).resolve()
        if not path.is_relative_to(self.output_root) or not path.is_file():
            raise RuntimeError("供应商临时输出不存在")
        content_type = (
            reference.declared_content_type
            or mimetypes.guess_type(reference.filename)[0]
            or "application/octet-stream"
        )
        return DownloadedProviderOutput(
            content=path.read_bytes(),
            content_type=content_type,
            filename=reference.filename,
        )
