from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from src.platform.provider_errors import ProviderRequestRejectedError
from src.utils.endpoints import get_provider_base_url

from .image import ImageGenModel
from .provider_result import ProviderGenerationResult


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_MAXIMUM_IMAGE_BYTES = 40 * 1024 * 1024
SUPPORTED_QUALITIES = frozenset({"auto", "low", "medium", "high"})
SUPPORTED_BACKGROUNDS = frozenset({"auto", "opaque", "transparent"})
logger = logging.getLogger(__name__)


class XlinksImageModel(ImageGenModel):
    """Synchronous Xlinks adapter for gpt-image-2 text-to-image calls."""

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(dict(config))
        self.api_key = str(config.get("api_key") or os.getenv("XLINKS_API_KEY") or "")
        self.base_url = str(
            config.get("base_url") or get_provider_base_url("XLINKS")
        ).rstrip("/")
        self.params = dict(config.get("params") or {})
        self.session = config.get("http_session") or requests.Session()
        self.connect_timeout = float(config.get("connect_timeout", 10))
        self.read_timeout = float(config.get("read_timeout", 180))
        self.maximum_image_bytes = int(
            config.get("maximum_image_bytes", DEFAULT_MAXIMUM_IMAGE_BYTES)
        )
        self.maximum_redirects = int(config.get("maximum_redirects", 3))
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        if not self.api_key:
            raise ValueError("XLINKS_API_KEY is not configured")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username:
            raise ValueError("XLINKS_BASE_URL must be an HTTPS origin")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("Xlinks request timeouts must be positive")
        if self.maximum_image_bytes <= len(PNG_SIGNATURE):
            raise ValueError("Xlinks image byte limit is too small")
        if self.maximum_redirects < 0 or self.maximum_redirects > 5:
            raise ValueError("Xlinks redirect limit is invalid")

    @staticmethod
    def _normalize_size(value: Any) -> str:
        text = str(value or "1024x1024").strip().lower().replace("*", "x")
        if text in {"auto", "square"}:
            return "1024x1024"
        if text in {"portrait", "9:16", "3:4"}:
            return "1024x1536"
        if text in {"landscape", "16:9", "4:3"}:
            return "1536x1024"
        if "x" not in text:
            raise ValueError("Xlinks image size is invalid")
        width_text, height_text = text.split("x", 1)
        try:
            width, height = int(width_text), int(height_text)
        except ValueError as exc:
            raise ValueError("Xlinks image size is invalid") from exc
        if width <= 0 or height <= 0:
            raise ValueError("Xlinks image size is invalid")
        if width == height:
            return "1024x1024"
        return "1536x1024" if width > height else "1024x1536"

    @staticmethod
    def _one(value: Any, *, name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"Xlinks {name} must be 1")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Xlinks {name} must be 1") from exc
        if normalized != 1:
            raise ValueError(f"Xlinks {name} must be 1")
        return normalized

    def _request_body(self, prompt: str, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("Xlinks image prompt cannot be empty")
        if kwargs.get("ref_image_path") or kwargs.get("ref_image_paths"):
            raise ValueError("Xlinks gpt-image-2 adapter supports image.t2i only")

        model = str(kwargs.get("model") or self.params.get("model_name") or "gpt-image-2")
        if model != "gpt-image-2":
            raise ValueError("Xlinks image adapter only supports gpt-image-2")
        count = kwargs.get("n", kwargs.get("count", kwargs.get("output_count", 1)))
        self._one(count, name="output count")
        output_format = str(kwargs.get("output_format") or "png").lower()
        if output_format != "png":
            raise ValueError("Xlinks image output_format must be png")
        quality = str(kwargs.get("quality") or self.params.get("quality") or "high").lower()
        if quality not in SUPPORTED_QUALITIES:
            raise ValueError("Xlinks image quality is unsupported")
        background = str(kwargs.get("background") or "auto").lower()
        if background not in SUPPORTED_BACKGROUNDS:
            raise ValueError("Xlinks image background is unsupported")
        size = self._normalize_size(
            kwargs.get("size", kwargs.get("resolution", self.params.get("size")))
        )
        return {
            "model": model,
            "prompt": normalized_prompt,
            "n": 1,
            "size": size,
            "quality": quality,
            "output_format": "png",
            "background": background,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
        logger.log(
            level,
            json.dumps(
                {"event": event, **fields},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        )

    @classmethod
    def _successful_response_for_log(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if key == "b64_json" and isinstance(item, str):
                    result[key] = {
                        "binary_omitted": True,
                        "encoded_chars": len(item),
                    }
                else:
                    result[key] = cls._successful_response_for_log(item)
            return result
        if isinstance(value, list):
            return [cls._successful_response_for_log(item) for item in value]
        return value

    @staticmethod
    def _response_text(response: Any) -> str:
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text
        content = getattr(response, "content", b"")
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content or "")

    def _safe_provider_error(self, response: Any) -> tuple[str, str]:
        code = f"HTTP_{getattr(response, 'status_code', 0)}"
        message = "Xlinks rejected the image request"
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
                code = str(raw_code)[:80]
            if raw_message:
                message = str(raw_message)[:400]
        if self.api_key:
            message = message.replace(self.api_key, "[REDACTED]")
        return code, message

    def _raise_for_status(self, response: Any) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        provider_code, safe_message = self._safe_provider_error(response)
        if 400 <= status < 500:
            safe_code = (
                "PROVIDER_AUTHENTICATION_FAILED"
                if status in {401, 403}
                else "PROVIDER_REQUEST_REJECTED"
            )
            raise ProviderRequestRejectedError(
                safe_message,
                provider_code=provider_code,
                safe_error_code=safe_code,
                safe_error_message=(
                    "Xlinks credential is invalid or unauthorized"
                    if status in {401, 403}
                    else "Xlinks rejected the image parameters"
                ),
            )
        raise RuntimeError(f"Xlinks image service unavailable ({provider_code})")

    @staticmethod
    def _request_id(response: Any) -> str | None:
        headers = getattr(response, "headers", {})
        value = headers.get("x-oneapi-request-id") or headers.get("x-request-id")
        if not value:
            return None
        return str(value).strip()[:255] or None

    @staticmethod
    def _extract_output(payload: Any) -> tuple[str, str]:
        if not isinstance(payload, Mapping):
            raise RuntimeError("Xlinks image response is not a JSON object")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
            raise RuntimeError("Xlinks image response must contain exactly one output")
        item = data[0]
        candidates = [
            ("b64_json", item.get("b64_json")),
            ("url", item.get("url")),
        ]
        usable = [
            (kind, value)
            for kind, value in candidates
            if isinstance(value, str) and value.strip()
        ]
        if len(usable) != 1:
            raise RuntimeError("Xlinks image response must contain one usable output")
        kind, value = usable[0]
        return kind, value.strip()

    def _decode_png(self, encoded: str) -> bytes:
        maximum_encoded = ((self.maximum_image_bytes + 2) // 3) * 4 + 8
        if len(encoded) > maximum_encoded:
            raise RuntimeError("Xlinks base64 image exceeds the size limit")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("Xlinks returned invalid base64 image data") from exc
        self._validate_png(content, declared_content_type=None)
        return content

    def _download_png(self, url: str) -> bytes:
        current_url = url
        for redirect_index in range(self.maximum_redirects + 1):
            parsed = urlparse(current_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username:
                raise RuntimeError("Xlinks image URL must use HTTPS")
            response = self.session.get(
                current_url,
                headers={"Accept": "image/png"},
                timeout=(self.connect_timeout, self.read_timeout),
                stream=True,
                allow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_index >= self.maximum_redirects:
                    raise RuntimeError("Xlinks image URL exceeded the redirect limit")
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError("Xlinks image redirect is missing Location")
                current_url = urljoin(current_url, location)
                continue
            self._raise_for_status(response)
            declared_length = response.headers.get("Content-Length")
            if declared_length:
                try:
                    if int(declared_length) > self.maximum_image_bytes:
                        raise RuntimeError("Xlinks image exceeds the size limit")
                except ValueError as exc:
                    raise RuntimeError("Xlinks image Content-Length is invalid") from exc
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.maximum_image_bytes:
                    raise RuntimeError("Xlinks image exceeds the size limit")
                chunks.append(chunk)
            content = b"".join(chunks)
            self._validate_png(content, response.headers.get("Content-Type"))
            return content
        raise RuntimeError("Xlinks image download did not complete")

    @staticmethod
    def _validate_png(content: bytes, declared_content_type: str | None) -> None:
        if not content.startswith(PNG_SIGNATURE):
            raise RuntimeError("Xlinks image output is not PNG")
        if declared_content_type:
            media_type = declared_content_type.split(";", 1)[0].strip().lower()
            if media_type not in {"image/png", "application/octet-stream"}:
                raise RuntimeError("Xlinks image content type is not PNG")

    @staticmethod
    def _atomic_write(output_path: str, content: bytes) -> None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def generate_with_usage(
        self,
        prompt: str,
        output_path: str,
        **kwargs: Any,
    ) -> ProviderGenerationResult:
        body = self._request_body(prompt, kwargs)
        callback = kwargs.get("on_provider_ids")
        started_at = time.perf_counter()
        request_url = f"{self.base_url}/images/generations"
        self._log_event(
            "xlinks.image_request",
            method="POST",
            url=request_url,
            request_body=body,
            timeout={
                "connect_seconds": self.connect_timeout,
                "read_seconds": self.read_timeout,
            },
        )
        try:
            response = self.session.post(
                request_url,
                headers=self._headers(),
                json=body,
                timeout=(self.connect_timeout, self.read_timeout),
            )
        except Exception as exc:
            self._log_event(
                "xlinks.image_transport_failed",
                level=logging.ERROR,
                method="POST",
                url=request_url,
                request_body=body,
                elapsed_seconds=round(time.perf_counter() - started_at, 3),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise

        request_id = self._request_id(response)
        response_json_error: Exception | None = None
        try:
            payload = response.json()
        except (TypeError, ValueError, requests.RequestException) as exc:
            payload = None
            response_json_error = exc
        response_body = (
            payload
            if int(response.status_code) >= 400
            else self._successful_response_for_log(payload)
        )
        if payload is None:
            response_body = self._response_text(response)
        self._log_event(
            "xlinks.image_response",
            level=(
                logging.INFO
                if 200 <= int(response.status_code) < 300
                else logging.ERROR
            ),
            method="POST",
            url=request_url,
            status_code=int(response.status_code),
            elapsed_seconds=round(time.perf_counter() - started_at, 3),
            provider_request_id=request_id,
            response_headers=dict(getattr(response, "headers", {}) or {}),
            response_body=response_body,
        )
        self._raise_for_status(response)
        if callable(callback):
            callback("xlinks", None, request_id)
        try:
            if response_json_error is not None:
                raise RuntimeError(
                    "Xlinks image response is not valid JSON"
                ) from response_json_error
            output_kind, output_value = self._extract_output(payload)
            content = (
                self._decode_png(output_value)
                if output_kind == "b64_json"
                else self._download_png(output_value)
            )
        except Exception as exc:
            self._log_event(
                "xlinks.image_processing_failed",
                level=logging.ERROR,
                method="POST",
                url=request_url,
                status_code=int(response.status_code),
                elapsed_seconds=round(time.perf_counter() - started_at, 3),
                provider_request_id=request_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise
        self._atomic_write(output_path, content)
        return ProviderGenerationResult(
            output_path=output_path,
            elapsed_seconds=max(time.perf_counter() - started_at, 0),
            raw_usage={
                "output_count": 1,
                "resolution": body["size"],
                "quality": body["quality"],
                "provider_request_id": request_id,
            },
        )

    def generate(self, prompt: str, output_path: str, **kwargs: Any) -> tuple[str, float]:
        result = self.generate_with_usage(prompt, output_path, **kwargs)
        return result.output_path, result.elapsed_seconds
