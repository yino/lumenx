from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urljoin

import requests
from pydantic import SecretStr

from src.models.provider_result import ProviderGenerationResult
from src.platform.provider_errors import (
    ProviderRequestRejectedError,
    ProviderTerminalFailureError,
)
from src.utils.endpoints import get_provider_base_url

from .interface import VideoGenerationRequest


DEFAULT_MAX_VIDEO_BYTES = 512 * 1024 * 1024
SUPPORTED_MODES = frozenset({"t2v", "i2v"})
SUPPORTED_RESOLUTIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "480p": (854, 480),
}
TRANSIENT_STATUSES = frozenset({404, 408, 409, 425, 429, 500, 502, 503, 504})


class XlinksGrokVideoProvider:
    """NewAPI-compatible Xlinks adapter for grok-imagine-video."""

    provider_name = "xlinks"

    def __init__(
        self,
        model_id: str,
        credential: SecretStr,
        *,
        session: requests.Session | None = None,
        base_url: str | None = None,
        connect_timeout: float = 10,
        read_timeout: float = 120,
        poll_interval: float = 15,
        max_wait_seconds: float = 900,
        maximum_video_bytes: int = DEFAULT_MAX_VIDEO_BYTES,
        maximum_redirects: int = 3,
    ) -> None:
        self.model_id = str(model_id).strip()
        self.api_key = credential.get_secret_value()
        self.base_url = str(
            base_url or get_provider_base_url("XLINKS")
        ).rstrip("/")
        self.session = session or requests.Session()
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.poll_interval = float(poll_interval)
        self.max_wait_seconds = float(max_wait_seconds)
        self.maximum_video_bytes = int(maximum_video_bytes)
        self.maximum_redirects = int(maximum_redirects)
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        if self.model_id != "grok-imagine-video":
            raise ValueError("Xlinks video adapter only supports grok-imagine-video")
        if not self.api_key:
            raise ValueError("XLINKS_API_KEY is not configured")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise ValueError("XLINKS_BASE_URL must be an HTTPS origin")
        if (
            self.connect_timeout <= 0
            or self.read_timeout <= 0
            or self.poll_interval <= 0
            or self.max_wait_seconds <= 0
        ):
            raise ValueError("Xlinks video timeouts must be positive")
        if self.maximum_video_bytes <= 0:
            raise ValueError("Xlinks video byte limit must be positive")
        if self.maximum_redirects < 0 or self.maximum_redirects > 5:
            raise ValueError("Xlinks video redirect limit is invalid")

    @staticmethod
    def _positive_integer(value: Any, *, name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Xlinks video {name} must be a positive integer")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Xlinks video {name} must be a positive integer") from exc
        if normalized <= 0:
            raise ValueError(f"Xlinks video {name} must be a positive integer")
        return normalized

    @staticmethod
    def _non_negative_integer(value: Any, *, name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Xlinks video {name} must be a non-negative integer")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Xlinks video {name} must be a non-negative integer") from exc
        if normalized < 0:
            raise ValueError(f"Xlinks video {name} must be a non-negative integer")
        return normalized

    @staticmethod
    def _resolution_dimensions(value: Any) -> tuple[int, int] | None:
        if value is None:
            return None
        normalized = str(value).strip().lower().replace(" ", "")
        if normalized in SUPPORTED_RESOLUTIONS:
            return SUPPORTED_RESOLUTIONS[normalized]
        if normalized in {"16:9", "16x9"}:
            return (1280, 720)
        if normalized in {"9:16", "9x16"}:
            return (720, 1280)
        if normalized in {"1:1", "1x1", "square"}:
            return (720, 720)
        if "x" not in normalized:
            raise ValueError("Xlinks video resolution is invalid")
        width_text, height_text = normalized.split("x", 1)
        width = XlinksGrokVideoProvider._positive_integer(width_text, name="width")
        height = XlinksGrokVideoProvider._positive_integer(height_text, name="height")
        return width, height

    @staticmethod
    def _copy_optional_integer(
        payload: dict[str, Any],
        parameters: Mapping[str, Any],
        name: str,
    ) -> None:
        value = parameters.get(name)
        if value is not None:
            payload[name] = XlinksGrokVideoProvider._positive_integer(value, name=name)

    def _request_body(self, request: VideoGenerationRequest) -> dict[str, Any]:
        mode = str(request.mode).strip().lower()
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"Xlinks video mode is unsupported: {request.mode}")
        prompt = str(request.prompt or "").strip()
        if not prompt:
            raise ValueError("Xlinks video prompt cannot be empty")

        parameters = dict(request.parameters)
        payload: dict[str, Any] = {
            "model": self.model_id,
            "prompt": prompt,
        }
        if mode == "i2v":
            if len(request.input_urls) != 1:
                raise ValueError("Xlinks i2v requires exactly one input image")
            image_url = str(request.input_urls[0]).strip()
            image_parsed = urlparse(image_url)
            if image_parsed.scheme != "https" or not image_parsed.netloc or image_parsed.username:
                raise ValueError("Xlinks video input image must use HTTPS")
            payload["image"] = image_url
        elif request.input_urls:
            raise ValueError("Xlinks t2v does not accept input images")

        output_count = parameters.get(
            "n", parameters.get("count", parameters.get("output_count", 1))
        )
        if self._positive_integer(output_count, name="output count") != 1:
            raise ValueError("Xlinks video adapter supports exactly one output")

        self._copy_optional_integer(payload, parameters, "duration")
        self._copy_optional_integer(payload, parameters, "fps")
        seed = parameters.get("seed")
        if seed is not None:
            payload["seed"] = self._non_negative_integer(seed, name="seed")

        width = parameters.get("width")
        height = parameters.get("height")
        if width is None or height is None:
            dimensions = self._resolution_dimensions(parameters.get("resolution"))
            if dimensions is not None:
                width, height = dimensions
        if width is not None:
            payload["width"] = self._positive_integer(width, name="width")
        if height is not None:
            payload["height"] = self._positive_integer(height, name="height")

        for name in ("response_format", "user"):
            value = parameters.get(name)
            if value is not None:
                text = str(value).strip()
                if text:
                    payload[name] = text

        metadata = parameters.get("metadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("Xlinks video metadata must be an object")
        metadata_payload = dict(metadata or {})
        for name in (
            "negative_prompt",
            "style",
            "quality_level",
            "watermark",
            "audio",
        ):
            if name in parameters and parameters[name] is not None:
                metadata_payload[name] = parameters[name]
        if metadata_payload:
            payload["metadata"] = metadata_payload
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _safe_error(self, response: Any) -> tuple[str, str]:
        provider_code = f"HTTP_{getattr(response, 'status_code', 0)}"
        message = "Xlinks rejected the video request"
        try:
            payload = response.json()
        except (TypeError, ValueError, requests.RequestException):
            payload = None
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, Mapping):
                raw_code = error.get("code") or error.get("type")
                raw_message = error.get("message")
            else:
                raw_code = payload.get("code")
                raw_message = payload.get("message")
            if raw_code:
                provider_code = str(raw_code)[:80]
            if raw_message:
                message = str(raw_message)[:400]
        return provider_code, message.replace(self.api_key, "[REDACTED]")

    def _raise_for_status(self, response: Any) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        provider_code, message = self._safe_error(response)
        if status in {401, 403}:
            raise ProviderRequestRejectedError(
                message,
                provider_code=provider_code,
                safe_error_code="PROVIDER_AUTHENTICATION_FAILED",
                safe_error_message="Xlinks credential is invalid or unauthorized",
            )
        if 400 <= status < 500:
            raise ProviderRequestRejectedError(
                message,
                provider_code=provider_code,
                safe_error_code="PROVIDER_REQUEST_REJECTED",
                safe_error_message="Xlinks rejected the video parameters",
            )
        raise RuntimeError(f"Xlinks video service unavailable ({provider_code})")

    @staticmethod
    def _request_id(response: Any) -> str | None:
        headers = getattr(response, "headers", {})
        value = headers.get("x-oneapi-request-id") or headers.get("x-request-id")
        if value:
            return str(value).strip()[:255] or None
        return None

    @staticmethod
    def _task_id(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            raise RuntimeError("Xlinks video creation response is not a JSON object")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise RuntimeError("Xlinks video creation response has no task_id")
        return task_id.strip()

    @staticmethod
    def _status_payload(payload: Any) -> tuple[str, str | None, Mapping[str, Any]]:
        if not isinstance(payload, Mapping):
            raise RuntimeError("Xlinks video status response is not a JSON object")
        status = str(payload.get("status") or "").strip().lower()
        url = payload.get("url") or payload.get("video_url")
        if not url and isinstance(payload.get("data"), Mapping):
            url = payload["data"].get("url") or payload["data"].get("video_url")
        return status, str(url).strip() if isinstance(url, str) and url.strip() else None, payload

    def _submit(self, request: VideoGenerationRequest) -> tuple[str, str | None]:
        try:
            response = self.session.post(
                f"{self.base_url}/video/generations",
                headers=self._headers(),
                json=self._request_body(request),
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                "Xlinks video submission connection failed; the remote task state is unknown"
            ) from exc
        self._raise_for_status(response)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Xlinks video creation response is not valid JSON") from exc
        request_id = self._request_id(response)
        if request_id is None and isinstance(payload.get("request_id"), str):
            request_id = payload["request_id"].strip()[:255] or None
        return self._task_id(payload), request_id

    def _poll(self, task_id: str) -> tuple[str, Mapping[str, Any]]:
        started_at = time.monotonic()
        last_connection_error: Exception | None = None
        while time.monotonic() - started_at < self.max_wait_seconds:
            try:
                response = self.session.get(
                    f"{self.base_url}/video/generations/{task_id}",
                    headers=self._headers(),
                    timeout=(self.connect_timeout, self.read_timeout),
                )
            except requests.RequestException as exc:
                last_connection_error = exc
                time.sleep(min(self.poll_interval * 2, 60))
                continue
            last_connection_error = None

            if response.status_code in {401, 403}:
                self._raise_for_status(response)
            if response.status_code in TRANSIENT_STATUSES:
                time.sleep(self.poll_interval)
                continue
            self._raise_for_status(response)
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Xlinks video status response is not valid JSON") from exc
            status, url, raw = self._status_payload(payload)
            if status == "completed":
                if not url:
                    raise RuntimeError("Xlinks completed video response has no url")
                return url, raw
            if status == "failed":
                error = raw.get("error")
                message = error.get("message") if isinstance(error, Mapping) else None
                raise ProviderTerminalFailureError(
                    str(message or "Xlinks video task failed")[:400],
                    provider_status=status,
                )
            if status not in {"queued", "in_progress"}:
                raise RuntimeError(f"Xlinks video task returned unknown status: {status or 'empty'}")
            time.sleep(self.poll_interval)

        if last_connection_error is not None:
            raise RuntimeError(
                f"Xlinks video task {task_id} polling was interrupted; the task id was retained"
            ) from last_connection_error
        raise RuntimeError(
            f"Xlinks video task {task_id} timed out after {self.max_wait_seconds:g}s; the task id was retained"
        )

    def _download(self, url: str, output_path: str) -> None:
        current_url = url
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        for redirect_index in range(self.maximum_redirects + 1):
            parsed = urlparse(current_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username:
                raise RuntimeError("Xlinks video URL must use HTTPS")
            try:
                response = self.session.get(
                    current_url,
                    headers={"Accept": "video/mp4"},
                    timeout=(self.connect_timeout, self.read_timeout),
                    stream=True,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise RuntimeError("Xlinks video download failed") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_index >= self.maximum_redirects:
                    raise RuntimeError("Xlinks video URL exceeded the redirect limit")
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError("Xlinks video redirect is missing Location")
                current_url = urljoin(current_url, location)
                continue
            self._raise_for_status(response)
            declared_length = response.headers.get("Content-Length")
            if declared_length:
                try:
                    if int(declared_length) > self.maximum_video_bytes:
                        raise RuntimeError("Xlinks video exceeds the size limit")
                except ValueError as exc:
                    raise RuntimeError("Xlinks video Content-Length is invalid") from exc
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".part", dir=target.parent
            )
            os.close(file_descriptor)
            temporary_path = Path(temporary_name)
            total = 0
            try:
                with temporary_path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.maximum_video_bytes:
                            raise RuntimeError("Xlinks video exceeds the size limit")
                        handle.write(chunk)
                if total < 12:
                    raise RuntimeError("Xlinks returned an empty or invalid MP4")
                with temporary_path.open("rb") as handle:
                    header = handle.read(64)
                if b"ftyp" not in header:
                    raise RuntimeError("Xlinks returned a non-MP4 video")
                os.replace(temporary_path, target)
                temporary_path = None
                return
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        raise RuntimeError("Xlinks video download did not return a final response")

    def generate(self, request: VideoGenerationRequest) -> ProviderGenerationResult:
        started_at = time.monotonic()
        task_id, request_id = self._submit(request)
        if request.on_provider_submission is not None:
            request.on_provider_submission("xlinks", task_id, request_id)
        video_url, status_payload = self._poll(task_id)
        self._download(video_url, request.output_path)
        parameters = dict(request.parameters)
        return ProviderGenerationResult(
            output_path=request.output_path,
            elapsed_seconds=time.monotonic() - started_at,
            raw_usage={
                "duration_seconds": parameters.get("duration"),
                "output_count": 1,
                "resolution": parameters.get("resolution")
                or (
                    f"{status_payload.get('metadata', {}).get('width')}x"
                    f"{status_payload.get('metadata', {}).get('height')}"
                    if isinstance(status_payload.get("metadata"), Mapping)
                    and status_payload["metadata"].get("width")
                    and status_payload["metadata"].get("height")
                    else None
                ),
                "provider_task_id": task_id,
            },
        )
