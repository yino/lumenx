from __future__ import annotations

import math
import re
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .credentials import is_sensitive_config_key


class AICapability(str, Enum):
    SCRIPT_ANALYSIS = "script.analysis"
    PROMPT_POLISH = "prompt.polish"
    IMAGE_T2I = "image.t2i"
    IMAGE_I2I = "image.i2i"
    VIDEO_T2V = "video.t2v"
    VIDEO_I2V = "video.i2v"
    VIDEO_R2V = "video.r2v"
    VIDEO_V2V = "video.v2v"
    SPEECH_TTS = "speech.tts"
    AUDIO_SFX = "audio.sfx"


class ParameterType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


ParameterScalar = str | int | float | bool


class ParameterRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value_type: ParameterType
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: list[ParameterScalar] | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "ParameterRule":
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("参数最小值不能大于最大值")
        if self.value_type not in {ParameterType.INTEGER, ParameterType.NUMBER} and (
            self.minimum is not None or self.maximum is not None
        ):
            raise ValueError("仅数值参数可以设置范围")
        if self.choices is not None and not self.choices:
            raise ValueError("参数候选值不能为空")
        return self

    def validate_value(self, value: Any) -> ParameterScalar:
        type_valid = {
            ParameterType.STRING: isinstance(value, str),
            ParameterType.INTEGER: isinstance(value, int) and not isinstance(value, bool),
            ParameterType.NUMBER: (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            ),
            ParameterType.BOOLEAN: isinstance(value, bool),
        }[self.value_type]
        if not type_valid:
            raise ValueError(f"参数 {self.name} 类型无效")
        if self.minimum is not None and float(value) < self.minimum:
            raise ValueError(f"参数 {self.name} 小于允许范围")
        if self.maximum is not None and float(value) > self.maximum:
            raise ValueError(f"参数 {self.name} 超出允许范围")
        if self.choices is not None and value not in self.choices:
            raise ValueError(f"参数 {self.name} 不在允许选项中")
        return value


class LLMTokenFormula(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["llm"]
    review_required: bool = False
    input_weight: int = Field(ge=0)
    output_weight: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class ImageTokenFormula(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["image"]
    review_required: bool = False
    base_tokens: int = Field(ge=0)
    per_image_tokens: int = Field(gt=0)
    max_images: int = Field(gt=0, le=16)
    resolution_multipliers: dict[str, int] = Field(min_length=1)
    option_multipliers: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_multipliers(self) -> "ImageTokenFormula":
        if any(value <= 0 for value in self.resolution_multipliers.values()):
            raise ValueError("图片分辨率倍率必须为正整数")
        if any(value <= 0 for value in self.option_multipliers.values()):
            raise ValueError("图片高级选项倍率必须为正整数")
        return self


class VideoTokenFormula(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["video"]
    review_required: bool = False
    base_tokens: int = Field(ge=0)
    tokens_per_second: int = Field(gt=0)
    max_duration_seconds: int = Field(gt=0, le=600)
    max_outputs: int = Field(gt=0, le=16)
    resolution_multipliers: dict[str, int] = Field(min_length=1)
    audio_multiplier: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def validate_multipliers(self) -> "VideoTokenFormula":
        if any(value <= 0 for value in self.resolution_multipliers.values()):
            raise ValueError("视频分辨率倍率必须为正整数")
        return self


class SpeechTokenFormula(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["speech"]
    review_required: bool = False
    unit: Literal["characters", "provider_tokens", "seconds"]
    tokens_per_unit: int = Field(gt=0)
    max_units: int = Field(gt=0)


MeteringFormula = Annotated[
    LLMTokenFormula | ImageTokenFormula | VideoTokenFormula | SpeechTokenFormula,
    Field(discriminator="kind"),
]


class FallbackPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    eligible_error_codes: list[str] = Field(default_factory=list)
    max_attempts: int = Field(default=1, ge=1, le=5)
    require_nonbillable_previous_attempt: bool = True

    @model_validator(mode="after")
    def validate_enabled_policy(self) -> "FallbackPolicy":
        if self.enabled and not self.eligible_error_codes:
            raise ValueError("启用回退时必须配置可回退错误码")
        if len(self.eligible_error_codes) != len(set(self.eligible_error_codes)):
            raise ValueError("可回退错误码不能重复")
        if any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", code)
            for code in self.eligible_error_codes
        ):
            raise ValueError("可回退错误码格式无效")
        return self


class ModelRouteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: AICapability
    display_name_zh: str = Field(min_length=1, max_length=160)
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    provider_model_id: str = Field(min_length=1, max_length=160)
    enabled: bool = False
    is_primary: bool = False
    priority: int = Field(default=100, ge=0)
    default_parameters: dict[str, ParameterScalar] = Field(default_factory=dict)
    parameter_schema: list[ParameterRule] = Field(default_factory=list)
    metering_formula: MeteringFormula
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)
    secret_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,159}$")

    @model_validator(mode="after")
    def validate_parameter_contract(self) -> "ModelRouteConfig":
        names = [rule.name for rule in self.parameter_schema]
        if len(names) != len(set(names)):
            raise ValueError("参数规则名称不能重复")
        if any(is_sensitive_config_key(name) for name in names):
            raise ValueError("模型参数不能声明凭据或密码字段")
        self.normalize_parameters({})
        return self

    def normalize_parameters(
        self,
        requested: dict[str, Any],
    ) -> dict[str, ParameterScalar]:
        rules = {rule.name: rule for rule in self.parameter_schema}
        unknown = set(requested) - set(rules)
        if unknown:
            raise ValueError(f"包含不受支持的参数：{', '.join(sorted(unknown))}")
        unknown_defaults = set(self.default_parameters) - set(rules)
        if unknown_defaults:
            raise ValueError(
                f"默认值包含未声明参数：{', '.join(sorted(unknown_defaults))}"
            )
        normalized = dict(self.default_parameters)
        normalized.update(requested)
        for rule in self.parameter_schema:
            if rule.required and rule.name not in normalized:
                raise ValueError(f"缺少必需参数：{rule.name}")
            if rule.name in normalized:
                normalized[rule.name] = rule.validate_value(normalized[rule.name])
        return normalized


class PlatformOperationalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signed_media_url_seconds: int = Field(default=300, ge=30, le=900)
    soft_delete_retention_days: int = Field(default=30, ge=1, le=365)
    stale_hold_minutes: int = Field(default=30, ge=1, le=1440)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_deployment_capacity(cls, value: Any) -> Any:
        if isinstance(value, dict) and "global_worker_concurrency" in value:
            normalized = dict(value)
            normalized.pop("global_worker_concurrency", None)
            return normalized
        return value


class RegistrationMode(str, Enum):
    DISABLED = "disabled"
    INVITE_ONLY = "invite_only"
    OPEN = "open"
    VERIFIED_OPEN = "verified_open"


class PlatformFeatureFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_mode: RegistrationMode = RegistrationMode.DISABLED
    new_ai_tasks_enabled: bool = False
    # Generic short-drama Agent rollout controls. Defaults are fail-closed so
    # existing storyboard/video routes remain unchanged until enabled.
    short_drama_agent_enabled: bool = False
    short_drama_agent_shadow_mode: bool = False
    short_drama_agent_require_approval: bool = True
    short_drama_agent_profiles: list[str] = Field(default_factory=lambda: ["grok-imagine-video"])
    short_drama_agent_max_concurrency: int = Field(default=2, ge=1, le=100)

    @model_validator(mode="before")
    @classmethod
    def migrate_registration_boolean(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "registration_enabled" not in value:
            return value
        normalized = dict(value)
        legacy_enabled = bool(normalized.pop("registration_enabled"))
        normalized.setdefault(
            "registration_mode",
            RegistrationMode.INVITE_ONLY if legacy_enabled else RegistrationMode.DISABLED,
        )
        return normalized

    @property
    def registration_enabled(self) -> bool:
        """Compatibility projection for internal callers during the rollout window."""
        return self.registration_mode is not RegistrationMode.DISABLED


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens_per_ticket: int = Field(gt=0)
    registration_initial_grant_microtickets: int = Field(default=0, ge=0)
    session_idle_seconds: int = Field(gt=0)
    session_absolute_seconds: int = Field(gt=0)
    max_sessions_per_user: int = Field(gt=0, le=100)
    max_ai_concurrency_per_user: int = Field(gt=0, le=100)
    exposed_capabilities: list[AICapability] = Field(default_factory=list)
    feature_flags: PlatformFeatureFlags = Field(default_factory=PlatformFeatureFlags)
    operational: PlatformOperationalSettings = Field(
        default_factory=PlatformOperationalSettings
    )

    @model_validator(mode="after")
    def validate_platform_limits(self) -> "PlatformConfig":
        if self.session_absolute_seconds < self.session_idle_seconds:
            raise ValueError("会话绝对有效期不能短于闲置有效期")
        if len(self.exposed_capabilities) != len(set(self.exposed_capabilities)):
            raise ValueError("开放能力不能重复")
        return self


class ConfigurationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
    platform: PlatformConfig
    routes: list[ModelRouteConfig]

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("创建配置草稿必须填写原因")
        return normalized

    @model_validator(mode="after")
    def validate_unique_routes(self) -> "ConfigurationDraft":
        keys = [
            (route.capability, route.provider, route.provider_model_id)
            for route in self.routes
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("同一配置版本不能包含重复模型路由")
        return self
