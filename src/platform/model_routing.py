from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from pydantic import SecretStr

from .configuration_schemas import AICapability, ModelRouteConfig
from .configuration_service import ConfigurationService, StoredConfiguration
from .contracts import CredentialProvider, ModelRouteSnapshot, UserContext
from .video_providers import ModelIdVideoProviderFactory, VideoProviderFactory


class ModelRouteUnavailableError(LookupError):
    pass


class ModelFallbackNotAllowedError(RuntimeError):
    pass


class ModelClientUnavailableError(LookupError):
    pass


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def thaw_snapshot_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_snapshot_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_snapshot_value(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class CapabilityRoutePlan:
    config_version_id: str
    capability: str
    tokens_per_ticket: int
    max_ai_concurrency_per_user: int
    routes: tuple[ModelRouteSnapshot, ...]

    @property
    def primary(self) -> ModelRouteSnapshot:
        if not self.routes:
            raise ModelRouteUnavailableError(f"能力 {self.capability} 没有可用模型路由")
        return self.routes[0]


class DatabaseModelConfigurationProvider:
    def __init__(
        self,
        configuration: ConfigurationService,
        runtime_identity: UserContext,
    ) -> None:
        self.configuration = configuration
        self.runtime_identity = runtime_identity

    @staticmethod
    def _route_id(
        configuration: StoredConfiguration,
        route: ModelRouteConfig,
    ) -> str:
        route_key = ":".join(
            (
                configuration.id,
                route.capability.value,
                route.provider,
                route.provider_model_id,
            )
        )
        return f"route_{hashlib.sha256(route_key.encode('utf-8')).hexdigest()[:24]}"

    @classmethod
    def _snapshot(
        cls,
        configuration: StoredConfiguration,
        route: ModelRouteConfig,
        requested_parameters: Mapping[str, Any],
    ) -> ModelRouteSnapshot:
        return ModelRouteSnapshot(
            config_version_id=configuration.id,
            route_id=cls._route_id(configuration, route),
            capability=route.capability.value,
            provider=route.provider,
            provider_model_id=route.provider_model_id,
            display_name=route.display_name_zh,
            parameters=_freeze(route.normalize_parameters(dict(requested_parameters))),
            metering_formula=_freeze(
                route.metering_formula.model_dump(mode="json")
            ),
            fallback_policy=_freeze(
                route.fallback_policy.model_dump(mode="json")
            ),
            secret_ref=route.secret_ref,
        )

    def build_plan(
        self,
        capability: str | AICapability,
        requested_parameters: Mapping[str, Any],
    ) -> CapabilityRoutePlan:
        try:
            normalized_capability = AICapability(capability)
        except (TypeError, ValueError) as exc:
            raise ModelRouteUnavailableError(f"不支持的 AI 能力：{capability}") from exc
        configuration = self.configuration.get_active_for_runtime(
            self.runtime_identity
        )
        candidates = [
            route
            for route in configuration.draft.routes
            if route.enabled and route.capability == normalized_capability
        ]
        primaries = [route for route in candidates if route.is_primary]
        if len(primaries) != 1:
            raise ModelRouteUnavailableError(
                f"能力 {normalized_capability.value} 没有唯一启用的主路由"
            )
        primary = primaries[0]
        fallbacks = sorted(
            (route for route in candidates if not route.is_primary),
            key=lambda route: (route.priority, route.provider, route.provider_model_id),
        )
        routes = (primary, *fallbacks)
        return CapabilityRoutePlan(
            config_version_id=configuration.id,
            capability=normalized_capability.value,
            tokens_per_ticket=configuration.draft.platform.tokens_per_ticket,
            max_ai_concurrency_per_user=(
                configuration.draft.platform.max_ai_concurrency_per_user
            ),
            routes=tuple(
                self._snapshot(configuration, route, requested_parameters)
                for route in routes
            ),
        )

    def select_route(
        self,
        capability: str,
        requested_parameters: Mapping[str, Any],
    ) -> ModelRouteSnapshot:
        return self.build_plan(capability, requested_parameters).primary

    @staticmethod
    def select_fallback(
        plan: CapabilityRoutePlan,
        *,
        current_route_id: str,
        error_code: str,
        previous_attempt_billable: bool,
        completed_attempts: int,
    ) -> ModelRouteSnapshot:
        try:
            current_index = next(
                index
                for index, route in enumerate(plan.routes)
                if route.route_id == current_route_id
            )
        except StopIteration as exc:
            raise ModelFallbackNotAllowedError(
                "当前模型不属于任务配置快照"
            ) from exc
        policy = plan.primary.fallback_policy
        if not policy.get("enabled"):
            raise ModelFallbackNotAllowedError("当前任务未启用模型回退")
        if error_code not in policy.get("eligible_error_codes", ()):
            raise ModelFallbackNotAllowedError("当前错误不允许切换备用模型")
        if previous_attempt_billable and policy.get(
            "require_nonbillable_previous_attempt",
            True,
        ):
            raise ModelFallbackNotAllowedError(
                "已产生计费结果，不能自动切换备用模型"
            )
        if completed_attempts >= int(policy.get("max_attempts", 1)):
            raise ModelFallbackNotAllowedError("模型尝试次数已达上限")
        next_index = current_index + 1
        if next_index >= len(plan.routes):
            raise ModelFallbackNotAllowedError("没有更多可用的备用模型")
        return plan.routes[next_index]


@dataclass(frozen=True, slots=True)
class RequestScopedModelClient:
    snapshot: ModelRouteSnapshot
    adapter: Any

    def execution_parameters(self) -> dict[str, Any]:
        parameters = thaw_snapshot_value(self.snapshot.parameters)
        parameters["model"] = self.snapshot.provider_model_id
        return parameters


ModelClientBuilder = Callable[[ModelRouteSnapshot, SecretStr], Any]


class RequestScopedModelClientFactory:
    def __init__(
        self,
        credential_provider: CredentialProvider,
        builders: Mapping[str, ModelClientBuilder] | None = None,
        video_providers: VideoProviderFactory | None = None,
    ) -> None:
        self.credential_provider = credential_provider
        self.builders = dict(builders or self._default_builders())
        self.video_providers = video_providers or ModelIdVideoProviderFactory(
            credential_provider
        )

    @staticmethod
    def _default_builders() -> dict[str, ModelClientBuilder]:
        return {
            "dashscope": RequestScopedModelClientFactory._build_dashscope,
            "mulerouter": RequestScopedModelClientFactory._build_mulerouter,
            "openai": RequestScopedModelClientFactory._build_openai,
        }

    @staticmethod
    def _adapter_config(
        snapshot: ModelRouteSnapshot,
        credential: SecretStr,
    ) -> dict[str, Any]:
        parameters = thaw_snapshot_value(snapshot.parameters)
        parameters.update(
            {
                "model_name": snapshot.provider_model_id,
                "i2i_model_name": snapshot.provider_model_id,
            }
        )
        return {
            "api_key": credential.get_secret_value(),
            "params": parameters,
        }

    @staticmethod
    def _build_dashscope(snapshot: ModelRouteSnapshot, credential: SecretStr) -> Any:
        config = RequestScopedModelClientFactory._adapter_config(snapshot, credential)
        if snapshot.capability.startswith("image."):
            from src.models.image import WanxImageModel

            return WanxImageModel(config)
        if snapshot.capability in {"script.analysis", "prompt.polish"}:
            from src.apps.comic_gen.llm_adapter import LLMAdapter

            return LLMAdapter(
                provider="dashscope",
                api_key=credential.get_secret_value(),
                model=snapshot.provider_model_id,
            )
        if snapshot.capability == "speech.tts":
            from src.audio.tts import TTSProcessor

            return TTSProcessor(
                api_key=credential.get_secret_value(),
                model=snapshot.provider_model_id,
            )
        raise ModelClientUnavailableError(
            f"DashScope 暂不支持能力 {snapshot.capability}"
        )

    @staticmethod
    def _build_mulerouter(
        snapshot: ModelRouteSnapshot,
        credential: SecretStr,
    ) -> Any:
        config = RequestScopedModelClientFactory._adapter_config(snapshot, credential)
        if snapshot.capability.startswith("image."):
            from src.models.mulerouter import MuleRouterImageModel

            return MuleRouterImageModel(config)
        raise ModelClientUnavailableError(
            f"MuleRouter 暂不支持能力 {snapshot.capability}"
        )

    @staticmethod
    def _build_openai(snapshot: ModelRouteSnapshot, credential: SecretStr) -> Any:
        if snapshot.capability not in {"script.analysis", "prompt.polish"}:
            raise ModelClientUnavailableError(
                f"OpenAI 兼容接口暂不支持能力 {snapshot.capability}"
            )
        from src.apps.comic_gen.llm_adapter import LLMAdapter

        return LLMAdapter(
            provider="openai",
            api_key=credential.get_secret_value(),
            model=snapshot.provider_model_id,
        )

    def create(self, snapshot: ModelRouteSnapshot) -> RequestScopedModelClient:
        if snapshot.capability.startswith("video."):
            return RequestScopedModelClient(
                snapshot=snapshot,
                adapter=self.video_providers.create(snapshot.provider_model_id),
            )
        builder = self.builders.get(snapshot.provider)
        if builder is None:
            raise ModelClientUnavailableError(
                f"模型提供商 {snapshot.provider} 没有服务端客户端"
            )
        credential = self.credential_provider.resolve(snapshot.secret_ref)
        adapter = builder(snapshot, credential)
        return RequestScopedModelClient(snapshot=snapshot, adapter=adapter)
