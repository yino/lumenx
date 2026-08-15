from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PositiveInt, ValidationError, field_validator, model_validator

from .ai_request_policy import ClientAIOverrideError, enforce_no_client_ai_overrides
from .configuration_schemas import AICapability, ParameterScalar
from .contracts import WorkspaceContext
from .credentials import reject_plaintext_secrets
from .identifiers import parse_database_id


ResourceKind = Literal[
    "series",
    "asset",
    "storyboard_frame",
    "voice",
    "template",
]

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_FORBIDDEN_REFERENCE_FIELDS = {
    "absolute_path",
    "audio_url",
    "file_path",
    "filesystem_path",
    "image_url",
    "local_path",
    "object_key",
    "oss_key",
    "path",
    "reference_image_url",
    "remote_url",
    "source_url",
    "url",
    "video_url",
}
_MEDIA_REQUIRED_CAPABILITIES = {
    AICapability.IMAGE_I2I,
    AICapability.VIDEO_I2V,
    AICapability.VIDEO_R2V,
    AICapability.VIDEO_V2V,
}


class AIGatewayContractError(ValueError):
    def __init__(self, code: str, message: str, *, paths: list[str] | None = None) -> None:
        self.code = code
        self.paths = tuple(paths or ())
        super().__init__(message)


def _normalized_field_name(value: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")


def _forbidden_reference_paths(value: Any, path: str = "请求") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if _normalized_field_name(key) in _FORBIDDEN_REFERENCE_FIELDS:
                paths.append(child_path)
            paths.extend(_forbidden_reference_paths(item, child_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            paths.extend(_forbidden_reference_paths(item, f"{path}[{index}]"))
    return paths


class AIGatewayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: AICapability
    idempotency_key: str = Field(min_length=1, max_length=160)
    project_id: PositiveInt | None = None
    resource_ids: dict[ResourceKind, list[PositiveInt]] = Field(default_factory=dict)
    media_ids: list[PositiveInt] = Field(default_factory=list, max_length=32)
    content: str | dict[str, JsonValue]
    parameters: dict[str, ParameterScalar] = Field(default_factory=dict, max_length=64)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
            raise ValueError("幂等键格式无效")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(
        cls,
        value: str | dict[str, JsonValue],
    ) -> str | dict[str, JsonValue]:
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("AI 请求内容不能为空")
            if len(normalized) > 500_000:
                raise ValueError("AI 请求内容过长")
            return normalized
        if not value:
            raise ValueError("AI 请求内容不能为空")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1_000_000:
            raise ValueError("AI 请求内容过大")
        return copy.deepcopy(value)

    @field_validator("resource_ids")
    @classmethod
    def validate_resource_ids(
        cls,
        value: dict[ResourceKind, list[int]],
    ) -> dict[ResourceKind, list[int]]:
        if len(value) > 8:
            raise ValueError("资源类型数量过多")
        total = 0
        for kind, identifiers in value.items():
            if not identifiers:
                raise ValueError(f"资源类型 {kind} 的标识不能为空")
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"资源类型 {kind} 包含重复标识")
            total += len(identifiers)
        if total > 128:
            raise ValueError("资源标识数量过多")
        return value

    @field_validator("media_ids")
    @classmethod
    def validate_media_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("媒体标识不能重复")
        return value

    @model_validator(mode="after")
    def validate_server_authority(self) -> "AIGatewayRequest":
        payload = self.model_dump(mode="json")
        try:
            enforce_no_client_ai_overrides(payload)
            reject_plaintext_secrets(payload, path="AI 请求")
        except ClientAIOverrideError as exc:
            raise ValueError(str(exc)) from exc
        paths = _forbidden_reference_paths(payload)
        if paths:
            raise ValueError("云端 AI 请求只允许使用已授权的资源和媒体标识")
        if self.capability in _MEDIA_REQUIRED_CAPABILITIES and not self.media_ids:
            raise ValueError("当前 AI 能力至少需要一个输入媒体标识")
        return self

    def reservation_payload(self) -> dict[str, Any]:
        return {
            "content": copy.deepcopy(self.content),
            "resource_ids": {
                kind: [str(identifier) for identifier in identifiers]
                for kind, identifiers in self.resource_ids.items()
            },
            "input_media_ids": [str(identifier) for identifier in self.media_ids],
            "parameters": copy.deepcopy(self.parameters),
        }


class AIGatewayRequestContract:
    @staticmethod
    def parse(
        context: WorkspaceContext,
        payload: Mapping[str, Any],
    ) -> AIGatewayRequest:
        try:
            parse_database_id(context.identity.user_id, field="用户 ID")
            parse_database_id(context.workspace_id, field="工作区 ID")
        except ValueError as exc:
            raise AIGatewayContractError(
                "AI_CONTEXT_INVALID",
                "AI 请求身份或工作区上下文无效",
            ) from exc
        if not context.identity.session_id or not context.identity.session_id.strip():
            raise AIGatewayContractError("AUTH_REQUIRED", "请先登录")

        try:
            enforce_no_client_ai_overrides(payload)
        except ClientAIOverrideError as exc:
            raise AIGatewayContractError(
                "AI_OVERRIDE_FORBIDDEN",
                str(exc),
                paths=exc.paths,
            ) from exc
        reference_paths = _forbidden_reference_paths(payload)
        if reference_paths:
            raise AIGatewayContractError(
                "AI_REFERENCE_FORBIDDEN",
                "云端 AI 请求只允许使用已授权的资源和媒体标识",
                paths=reference_paths,
            )
        try:
            return AIGatewayRequest.model_validate(dict(payload))
        except ValidationError as exc:
            messages = [str(error.get("msg", "")) for error in exc.errors()]
            chinese = next(
                (
                    message.removeprefix("Value error, ")
                    for message in messages
                    if any("\u4e00" <= character <= "\u9fff" for character in message)
                ),
                "AI 请求参数无效",
            )
            raise AIGatewayContractError("AI_REQUEST_INVALID", chinese) from exc
