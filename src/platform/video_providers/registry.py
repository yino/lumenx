from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pydantic import SecretStr

from ..contracts import CredentialProvider
from .aliyun import AliyunWanVideoProvider
from .interface import VideoProvider
from .volcengine import VolcengineSeedanceProvider


class VideoProviderUnavailableError(LookupError):
    pass


VideoProviderBuilder = Callable[[str, SecretStr], VideoProvider]


@dataclass(frozen=True, slots=True)
class VideoProviderRegistration:
    name: str
    model_prefixes: tuple[str, ...]
    secret_ref: str
    builder: VideoProviderBuilder

    def matches(self, model_id: str) -> bool:
        return any(model_id.startswith(prefix) for prefix in self.model_prefixes)


DEFAULT_VIDEO_PROVIDERS = (
    VideoProviderRegistration(
        name="volcengine-seedance",
        model_prefixes=("seedance-2.0-", "seedance/seedance-"),
        secret_ref="ARK_API_KEY",
        builder=VolcengineSeedanceProvider,
    ),
    VideoProviderRegistration(
        name="aliyun-wan",
        model_prefixes=("wan2.", "wan/wan"),
        secret_ref="DASHSCOPE_API_KEY",
        builder=AliyunWanVideoProvider,
    ),
)


class ModelIdVideoProviderFactory:
    """Resolves a provider implementation using only the logical model ID."""

    def __init__(
        self,
        credential_provider: CredentialProvider,
        registrations: Iterable[VideoProviderRegistration] | None = None,
    ) -> None:
        self.credential_provider = credential_provider
        self.registrations = tuple(
            DEFAULT_VIDEO_PROVIDERS if registrations is None else registrations
        )

    def create(self, model_id: str) -> VideoProvider:
        normalized = str(model_id).strip()
        if not normalized:
            raise VideoProviderUnavailableError("视频模型 ID 不能为空")
        matches = [
            registration
            for registration in self.registrations
            if registration.matches(normalized)
        ]
        if not matches:
            raise VideoProviderUnavailableError(
                f"视频模型 {normalized} 尚未实现供应商适配器"
            )
        if len(matches) > 1:
            names = ", ".join(registration.name for registration in matches)
            raise VideoProviderUnavailableError(
                f"视频模型 {normalized} 匹配到多个供应商适配器：{names}"
            )
        registration = matches[0]
        credential = self.credential_provider.resolve(registration.secret_ref)
        return registration.builder(normalized, credential)
