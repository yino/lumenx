#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.models.xlinks import XlinksImageModel
from src.utils.endpoints import get_provider_base_url


class RecordingSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        self.generation_metadata: dict[str, Any] = {}

    def post(self, url, **kwargs):
        response = super().post(url, **kwargs)
        payload: Any = None
        try:
            payload = response.json()
        except (TypeError, ValueError, requests.RequestException):
            pass
        data = payload.get("data") if isinstance(payload, dict) else None
        item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        self.generation_metadata = {
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "request_id_present": bool(
                response.headers.get("x-oneapi-request-id")
                or response.headers.get("x-request-id")
            ),
            "response_shape": {
                "data_is_list": isinstance(data, list),
                "data_count": len(data) if isinstance(data, list) else None,
                "has_b64_json": bool(item.get("b64_json")),
                "has_url": bool(item.get("url")),
            },
        }
        return response


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one redacted Xlinks gpt-image-2 T2I contract smoke test"
    )
    parser.add_argument(
        "--prompt",
        default="A single red apple on a neutral gray studio background",
    )
    args = parser.parse_args()

    api_key = os.getenv("XLINKS_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("XLINKS_API_KEY is not configured in the process environment")
    base_url = get_provider_base_url("XLINKS")
    session = RecordingSession()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    models_started = time.perf_counter()
    models_response = session.get(
        f"{base_url}/models",
        headers=headers,
        timeout=(10, 30),
    )
    models_elapsed = time.perf_counter() - models_started
    model_ids: list[str] = []
    try:
        models_payload = models_response.json()
        if isinstance(models_payload, dict) and isinstance(models_payload.get("data"), list):
            model_ids = [
                str(item.get("id"))
                for item in models_payload["data"]
                if isinstance(item, dict) and item.get("id")
            ]
    except (TypeError, ValueError, requests.RequestException):
        pass
    if models_response.status_code >= 400:
        raise SystemExit(f"Xlinks GET /models failed with HTTP {models_response.status_code}")

    output_root = Path(tempfile.mkdtemp(prefix="lumenx-xlinks-smoke-"))
    output_path = output_root / "gpt-image-2.png"
    provider_ids: list[tuple[str, str | None, str | None]] = []
    adapter = XlinksImageModel(
        {
            "api_key": api_key,
            "base_url": base_url,
            "http_session": session,
        }
    )
    generation_started = time.perf_counter()
    result = adapter.generate_with_usage(
        args.prompt,
        str(output_path),
        model="gpt-image-2",
        size="1024x1024",
        quality="low",
        n=1,
        output_format="png",
        background="auto",
        on_provider_ids=lambda provider, task_id, request_id: provider_ids.append(
            (provider, task_id, request_id)
        ),
    )
    generation_elapsed = time.perf_counter() - generation_started

    report = {
        "base_url": base_url,
        "models": {
            "status_code": models_response.status_code,
            "content_type": models_response.headers.get("Content-Type"),
            "request_id_present": bool(
                models_response.headers.get("x-oneapi-request-id")
                or models_response.headers.get("x-request-id")
            ),
            "elapsed_seconds": round(models_elapsed, 3),
            "gpt_image_2_present": "gpt-image-2" in model_ids,
            "model_count": len(model_ids),
        },
        "generation": {
            **session.generation_metadata,
            "elapsed_seconds": round(generation_elapsed, 3),
            "provider_callback_recorded": bool(provider_ids),
            "output_is_png": output_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
            "output_bytes": output_path.stat().st_size,
            "temporary_output": str(output_path),
            "usage_keys": sorted(result.raw_usage),
        },
        "billing_observation": "No billing fields were exposed by the public response.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
