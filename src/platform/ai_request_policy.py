from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel


_FORBIDDEN_OVERRIDE_FIELDS = {
    "api_key",
    "base_url",
    "credential",
    "credentials",
    "endpoint",
    "endpoint_url",
    "metering_formula",
    "model",
    "model_id",
    "model_name",
    "model_settings",
    "polish_model",
    "price",
    "pricing",
    "provider",
    "provider_model_id",
    "secret_ref",
    "target_model",
    "tokens_per_ticket",
}
_HISTORICAL_EXECUTION_FIELDS = {"model_settings", "polish_model"}


class ClientAIOverrideError(ValueError):
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(
            "云端 AI 请求不允许指定模型、提供商、端点、价格或凭据"
        )


def _normalized_field_name(value: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")


def _forbidden_paths(value: Any, path: str = "请求") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if _normalized_field_name(key) in _FORBIDDEN_OVERRIDE_FIELDS:
                paths.append(child_path)
            paths.extend(_forbidden_paths(item, child_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            paths.extend(_forbidden_paths(item, f"{path}[{index}]"))
    return paths


def enforce_no_client_ai_overrides(payload: Any) -> None:
    paths = _forbidden_paths(payload)
    if paths:
        raise ClientAIOverrideError(paths)


async def enforce_cloud_ai_request(request: Request) -> None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        return
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "AI_REQUEST_INVALID", "message": "AI 请求格式无效"},
        ) from exc
    try:
        enforce_no_client_ai_overrides(payload)
    except ClientAIOverrideError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "AI_OVERRIDE_FORBIDDEN", "message": str(exc)},
        ) from exc


def cloud_execution_payload(document: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(document, BaseModel):
        payload = document.model_dump(mode="json")
    else:
        payload = copy.deepcopy(dict(document))

    def strip_historical_fields(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): strip_historical_fields(item)
                for key, item in value.items()
                if _normalized_field_name(str(key))
                not in _HISTORICAL_EXECUTION_FIELDS
            }
        if isinstance(value, list):
            return [strip_historical_fields(item) for item in value]
        return copy.deepcopy(value)

    return strip_historical_fields(payload)
