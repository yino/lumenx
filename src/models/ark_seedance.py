"""Seedance 2.0 video generation through Volcengine Ark.

Supports both the Agent Plan endpoint (``/api/plan/v3``) and the standard
pay-as-you-go endpoint (``/api/v3``). Both expose the same asynchronous video
task protocol; only the base URL, API key, and model identifier differ.
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .base import VideoGenModel
from ..platform.provider_errors import ProviderRequestRejectedError
from ..utils.endpoints import get_provider_base_url
from ..utils.oss_utils import OSSImageUploader
from ..utils.provider_media import resolve_media_input

logger = logging.getLogger(__name__)

AGENT_PLAN_MODEL = "doubao-seedance-2.0"
STANDARD_MODEL = "doubao-seedance-2-0-260128"


class ArkSeedanceVideoModel(VideoGenModel):
    """Volcengine Ark adapter for Seedance T2V, I2V, and R2V generation."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        params = config.get("params", {})
        self.api_key = config.get("api_key") or os.getenv("ARK_API_KEY", "")
        self.base_url = (
            config.get("base_url")
            or get_provider_base_url("ARK")
        ).rstrip("/")
        self.model_name = (
            params.get("model_name")
            or os.getenv("ARK_SEEDANCE_MODEL")
            or self._default_model_for_base_url(self.base_url)
        )
        self.poll_interval = float(
            config.get("poll_interval")
            or os.getenv("ARK_VIDEO_POLL_INTERVAL", "5")
        )
        self.max_wait = int(
            config.get("max_wait")
            or os.getenv("ARK_VIDEO_MAX_WAIT_SECONDS", "1800")
        )

    @staticmethod
    def _default_model_for_base_url(base_url: str) -> str:
        if "/api/plan/" in f"{base_url.rstrip('/')}/":
            return AGENT_PLAN_MODEL
        return STANDARD_MODEL

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise RuntimeError(
                "ARK_API_KEY is not configured. Use an Agent Plan API Key for "
                "/api/plan/v3, or a standard Ark API Key for /api/v3."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _response_json(response, *, action: str) -> Dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            body = (getattr(response, "text", "") or "")[:1000]
            raise RuntimeError(
                f"Ark Seedance {action} returned invalid JSON "
                f"(HTTP {response.status_code}): {body}"
            ) from exc

        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = error.get("code") or error.get("type") or response.status_code
                message = error.get("message") or str(error)
            else:
                code = payload.get("code", response.status_code) if isinstance(payload, dict) else response.status_code
                message = payload.get("message", str(payload)) if isinstance(payload, dict) else str(payload)
            # Keep the provider's actionable rejection in worker logs while the
            # task API continues to expose only the safe generic error. Do not
            # log request content, media URLs, or authorization headers here.
            logger.warning(
                "[Ark/Seedance] %s rejected (http=%s, code=%s, message=%s)",
                action,
                response.status_code,
                str(code)[:120],
                str(message)[:500],
            )
            detail = f"{code} - {message}"
            # Ark returns HTTP 404/UnsupportedModel when a normal Ark model
            # (or a model not enabled for the account) is sent to the Agent
            # Plan endpoint. Include the exact remediation in the error so a
            # failed storyboard task is actionable from the backend log/UI.
            response_url = str(getattr(response, "url", "") or "")
            if str(code).lower() == "unsupportedmodel" and "/api/plan/" in response_url:
                detail = (
                    f"{detail}. 当前 API Key/套餐未启用该 Agent Plan 视频模型；"
                    "请在火山方舟控制台为 Agent Plan 项目开通并选择可用的 Seedance 视频模型，"
                    "然后将其真实模型名/接入点 ID 填入 ARK_SEEDANCE_MODEL。"
                    "若使用普通按量方舟，请改用 ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3，"
                    f"并使用普通模型标识 {STANDARD_MODEL} 及对应的普通方舟 API Key。"
                )
            elif (
                str(code).lower() == "invalidparameter"
                and "does not support content generation" in str(message).lower()
            ):
                detail = (
                    f"{detail}. 当前 ARK_SEEDANCE_MODEL 不是视频生成模型；"
                    "请使用控制台中支持内容生成的视频模型，Agent Plan 通常为 "
                    f"{AGENT_PLAN_MODEL}，普通方舟通常为 {STANDARD_MODEL}。"
                )
            normalized_code = str(code).lower()
            if normalized_code == "modelnotopen":
                raise ProviderRequestRejectedError(
                    f"Ark Seedance {action} failed (HTTP {response.status_code}): {detail}",
                    provider_code=str(code),
                    safe_error_code="PROVIDER_MODEL_NOT_OPEN",
                    safe_error_message=(
                        "当前视频模型尚未在火山方舟账号开通，请开通模型后重试"
                    ),
                )
            if normalized_code.startswith("inputimagesensitivecontentdetected"):
                raise ProviderRequestRejectedError(
                    f"Ark Seedance {action} failed (HTTP {response.status_code}): {detail}",
                    provider_code=str(code),
                    safe_error_code="PROVIDER_INPUT_SENSITIVE_CONTENT",
                    safe_error_message=(
                        "参考图片可能包含真人或隐私信息，请更换为插画/动漫图片后重试"
                    ),
                )
            raise RuntimeError(
                f"Ark Seedance {action} failed (HTTP {response.status_code}): {detail}"
            )

        if not isinstance(payload, dict):
            raise RuntimeError(f"Ark Seedance {action} returned a non-object response")
        return payload

    def _resolve_image(self, ref: str, *, model_name: str) -> str:
        if ref.startswith(("http://", "https://", "data:")):
            return ref
        return resolve_media_input(
            ref,
            model_name=model_name,
            modality="image",
            backend="ark",
            uploader=OSSImageUploader(),
        ).value

    def _collect_images(
        self,
        *,
        img_url: Optional[str],
        img_path: Optional[str],
        ref_image_urls: List[str],
        model_name: str,
    ) -> List[str]:
        refs: List[str] = []
        has_provider_ready_url = isinstance(img_url, str) and img_url.startswith(
            ("http://", "https://", "data:")
        )
        primary = img_url if has_provider_ready_url else (img_path or img_url)
        if primary:
            refs.append(primary)
        refs.extend(ref for ref in ref_image_urls if isinstance(ref, str) and ref.strip())

        resolved: List[str] = []
        seen = set()
        for ref in refs:
            if ref in seen:
                continue
            seen.add(ref)
            resolved.append(self._resolve_image(ref, model_name=model_name))
        return resolved

    @staticmethod
    def _normalize_duration(value: Any) -> int:
        """Apply Seedance 2.0's documented 4-15 second duration range.

        Older storyboard frames can retain a duration such as 3 seconds even
        after the model catalog is updated. Normalize at the provider boundary
        so stale persisted UI state cannot make an otherwise valid request fail.
        """
        try:
            duration = int(value) if value is not None else 5
        except (TypeError, ValueError):
            duration = 5
        return max(4, min(15, duration))

    def _build_request_body(
        self,
        *,
        prompt: str,
        img_url: Optional[str] = None,
        img_path: Optional[str] = None,
        generation_mode: str = "",
        ref_image_urls: Optional[List[str]] = None,
        duration: int = 5,
        resolution: Optional[str] = "720p",
        ratio: Optional[str] = "16:9",
        seed: Optional[int] = None,
        watermark: bool = False,
        generate_audio: bool = False,
    ) -> Dict[str, Any]:
        mode = (generation_mode or "").strip().lower()
        references = list(ref_image_urls or [])
        if not mode:
            mode = "r2v" if references else ("i2v" if img_url or img_path else "t2v")

        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt or ""}]
        if mode == "i2v":
            images = self._collect_images(
                img_url=img_url,
                img_path=img_path,
                ref_image_urls=[],
                model_name="seedance-2.0-i2v",
            )
            if not images:
                raise ValueError("Seedance I2V requires an input image")
            content.append(
                {"type": "image_url", "image_url": {"url": images[0]}}
            )
        elif mode == "r2v":
            images = self._collect_images(
                img_url=img_url,
                img_path=img_path,
                ref_image_urls=references,
                model_name="seedance-2.0-r2v",
            )
            if not images:
                raise ValueError("Seedance R2V requires at least one reference image")
            if len(images) > 9:
                raise ValueError("Seedance 2.0 R2V supports at most 9 reference images")
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": image},
                    "role": "reference_image",
                }
                for image in images
            )
        elif mode != "t2v":
            raise ValueError(f"Unsupported Seedance generation mode: {mode}")

        requested_duration = duration
        duration = self._normalize_duration(duration)
        if requested_duration != duration:
            logger.warning(
                "[Ark/Seedance] Adjusting invalid duration %r to %ss; "
                "Seedance 2.0 supports 4-15 seconds",
                requested_duration,
                duration,
            )

        body: Dict[str, Any] = {
            "model": self.model_name,
            "content": content,
            "generate_audio": bool(generate_audio),
            "duration": duration,
            "watermark": bool(watermark),
        }
        if ratio:
            body["ratio"] = ratio
        if resolution:
            body["resolution"] = resolution.lower()
        if seed is not None:
            body["seed"] = int(seed)
        return body

    def generate(
        self,
        prompt: str,
        output_path: str,
        img_url: Optional[str] = None,
        img_path: Optional[str] = None,
        **kwargs,
    ) -> Tuple[str, float]:
        start_time = time.time()
        body = self._build_request_body(
            prompt=prompt,
            img_url=img_url,
            img_path=img_path,
            generation_mode=kwargs.get("generation_mode", ""),
            ref_image_urls=kwargs.get("ref_image_urls"),
            duration=kwargs.get("duration", 5),
            resolution=kwargs.get("resolution", "720p"),
            ratio=kwargs.get("aspect_ratio") or kwargs.get("ratio") or "16:9",
            seed=kwargs.get("seed"),
            watermark=kwargs.get("watermark", False),
            generate_audio=kwargs.get("generate_audio", kwargs.get("audio", False)),
        )

        tasks_url = f"{self.base_url}/contents/generations/tasks"
        logger.info(
            "[Ark/Seedance] Submitting %s task (model=%s, base=%s)",
            kwargs.get("generation_mode") or "auto",
            self.model_name,
            self.base_url,
        )
        response = requests.post(tasks_url, headers=self._headers(), json=body, timeout=60)
        result = self._response_json(response, action="task creation")
        task_id = result.get("id")
        if not task_id:
            raise RuntimeError(f"Ark Seedance task creation returned no id: {result}")

        request_id = None
        response_headers = getattr(response, "headers", None)
        if response_headers:
            request_id = response_headers.get("x-request-id") or response_headers.get("X-Request-Id")
        callback = kwargs.get("on_provider_ids")
        if callable(callback):
            callback("volcengine-ark", task_id, request_id)

        poll_url = f"{tasks_url}/{task_id}"
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            poll_response = requests.get(poll_url, headers=self._headers(), timeout=60)
            task = self._response_json(poll_response, action="task query")
            status = str(task.get("status") or "").lower()
            logger.info("[Ark/Seedance] Task %s status: %s", task_id, status or "unknown")

            if status == "succeeded":
                content = task.get("content") or {}
                video_url = content.get("video_url") if isinstance(content, dict) else None
                if not video_url:
                    raise RuntimeError(f"Ark Seedance task succeeded without content.video_url: {task}")
                self._download_video(video_url, output_path)
                elapsed = time.time() - start_time
                logger.info("[Ark/Seedance] Done in %.1fs -> %s", elapsed, output_path)
                return output_path, elapsed

            if status in {"failed", "expired", "cancelled", "canceled"}:
                error = task.get("error") or {}
                if isinstance(error, dict):
                    code = error.get("code") or status
                    message = error.get("message") or str(error)
                else:
                    code, message = status, str(error)
                raise RuntimeError(f"Ark Seedance task failed: {code} - {message}")

            time.sleep(self.poll_interval)

        raise RuntimeError(f"Ark Seedance task timed out after {self.max_wait}s (task_id={task_id})")

    @staticmethod
    def _download_video(url: str, output_path: str) -> None:
        response = requests.get(url, stream=True, timeout=300)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Ark Seedance video download failed (HTTP {response.status_code})"
            )
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(output_path, "wb") as file_obj:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    file_obj.write(chunk)
