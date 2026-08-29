"""Model profiles for provider-neutral short-drama production."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import AgentRouteError
from ..core.contracts import AgentRunState
from ...platform.contracts import ModelRouteSnapshot


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    modes: frozenset[str] = frozenset({"t2v"})
    duration_min: float = Field(default=1, gt=0)
    duration_max: float = Field(default=10, gt=0)
    resolutions: frozenset[str] = frozenset()
    default_resolution: str = "720p"
    max_reference_images: int = Field(default=0, ge=0)
    supports_audio: bool = False
    input_field: str = "images"


class PromptRendering(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix: str = ""
    reference_instruction: str = (
        "参考素材 {order}（{alias}）：仅用于{purpose}。"
    )
    suffix: str = ""
    transport_field: str = "images"


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=200)
    capabilities: ModelCapabilities
    rendering: PromptRendering
    enabled: bool = True
    aliases: tuple[str, ...] = ()
    route_snapshot: dict[str, Any] | None = None


@dataclass
class ProfileResolver:
    """Resolve a profile through a server-side allowlist and route snapshot."""

    profiles: Mapping[str, ModelProfile] | None = None
    route_resolver: Callable[[str, Mapping[str, Any]], ModelRouteSnapshot] | None = None
    active_model_ids: set[str] | None = None
    catalog_path: str | Path | None = None
    _loaded: dict[str, ModelProfile] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._loaded = dict(self.profiles or default_profiles())
        if self.catalog_path:
            self._load_catalog(Path(self.catalog_path))
        # Alias lookup is intentionally server-side and immutable per resolver.
        for profile in tuple(self._loaded.values()):
            for alias in profile.aliases:
                self._loaded.setdefault(alias, profile)

    def _load_catalog(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for model_id, entry in (raw.get("models") or {}).items():
            if not isinstance(entry, Mapping):
                continue
            capabilities = set(entry.get("capabilities") or ())
            if not capabilities:
                continue
            params = entry.get("params") or {}
            duration = entry.get("duration") or {}
            inputs = entry.get("inputs") or {}
            try:
                profile = ModelProfile(
                    profile_id=str(model_id),
                    model_id=str(model_id),
                    provider=str(entry.get("provider") or _provider_for_model(model_id)),
                    display_name=str(entry.get("display_name") or model_id),
                    capabilities=ModelCapabilities(
                        modes=frozenset(capabilities),
                        duration_min=float(duration.get("min", 1)),
                        duration_max=float(duration.get("max", 10)),
                        resolutions=frozenset((params.get("resolution") or {}).get("options", ())),
                        default_resolution=str((params.get("resolution") or {}).get("default", "720p")),
                        max_reference_images=int((inputs.get("reference_images") or {}).get("max", 0)),
                        supports_audio=bool(params.get("audio", params.get("sound", False))),
                        input_field="images" if "r2v" in capabilities else "image_url",
                    ),
                    rendering=PromptRendering(transport_field="images" if "r2v" in capabilities else "image_url"),
                    enabled=str(entry.get("status", "active")) == "active",
                )
            except (TypeError, ValueError):
                continue
            self._loaded[profile.profile_id] = profile

    def resolve(
        self,
        requested: str | None,
        *,
        generation_mode: str = "r2v",
        parameters: Mapping[str, Any] | None = None,
    ) -> ModelProfile:
        key = (requested or "grok-imagine-video").strip()
        profile = self._loaded.get(key)
        if profile is None:
            profile = self._resolve_legacy(key, generation_mode)
        if profile is None:
            raise AgentRouteError(f"模型 Profile {key} 不存在", code="MODEL_PROFILE_UNKNOWN")
        if not profile.enabled:
            raise AgentRouteError(f"模型 Profile {key} 已禁用", code="MODEL_ROUTE_DISABLED")
        if generation_mode not in profile.capabilities.modes:
            raise AgentRouteError(
                f"模型 {profile.model_id} 不支持 {generation_mode}",
                code="MODEL_CAPABILITY_UNSUPPORTED",
            )
        if self.active_model_ids is not None and profile.model_id not in self.active_model_ids:
            raise AgentRouteError(
                f"模型 {profile.model_id} 不在服务端 allowlist 中",
                code="MODEL_ROUTE_UNAVAILABLE",
            )
        route_snapshot: ModelRouteSnapshot | None = None
        if self.route_resolver:
            try:
                route_snapshot = self.route_resolver(
                    f"video.{generation_mode}",
                    {"model_choice": profile.model_id, **dict(parameters or {})},
                )
            except Exception as exc:
                raise AgentRouteError(str(exc), code="MODEL_ROUTE_UNAVAILABLE") from exc
            if route_snapshot.provider_model_id != profile.model_id:
                raise AgentRouteError("服务端路由未接受请求模型", code="MODEL_ROUTE_REWRITTEN")
            if route_snapshot.provider != profile.provider:
                # Catalog may use an adapter family name while route snapshots
                # identify a concrete backend. Capture the actual provider.
                profile = profile.model_copy(
                    update={"provider": route_snapshot.provider, "route_snapshot": _snapshot_dict(route_snapshot)}
                )
        elif profile.route_snapshot is not None:
            route_snapshot = None
        return profile

    def _resolve_legacy(self, value: str, mode: str) -> ModelProfile | None:
        normalized = value.lower()
        if normalized in {"seedance-short-drama", "seedance", "seedance-2.0", "seedance-2.0-r2v"}:
            profile = self._loaded.get("seedance/seedance-2.0-video") or self._loaded.get("seedance-2.0-r2v")
            if profile:
                return profile
        if normalized in {"grok-imagine-video", "xlinks-grok-video/grok-imagine-video#i2v"}:
            return self._loaded.get("grok-imagine-video")
        # Canonical mode IDs use the part before '#'.
        if "#" in value:
            return self._loaded.get(value.split("#", 1)[0])
        return None


def _provider_for_model(model_id: str) -> str:
    if "grok" in model_id:
        return "xlinks"
    if "seedance" in model_id:
        return "ark"
    return "server"


def _snapshot_dict(snapshot: ModelRouteSnapshot) -> dict[str, Any]:
    return {
        "config_version_id": snapshot.config_version_id,
        "route_id": snapshot.route_id,
        "capability": snapshot.capability,
        "provider": snapshot.provider,
        "provider_model_id": snapshot.provider_model_id,
        "display_name": snapshot.display_name,
        "parameters": dict(snapshot.parameters),
    }


def default_profiles() -> dict[str, ModelProfile]:
    return {
        "seedance/seedance-2.0-video": ModelProfile(
            profile_id="seedance/seedance-2.0-video",
            model_id="seedance/seedance-2.0-video",
            provider="ark",
            display_name="Seedance 2.0",
            aliases=("seedance-short-drama", "seedance", "seedance-2.0-r2v"),
            capabilities=ModelCapabilities(
                modes=frozenset({"t2v", "i2v", "r2v"}),
                duration_min=4,
                duration_max=15,
                resolutions=frozenset({"720p", "1080p"}),
                default_resolution="1080p",
                max_reference_images=9,
                supports_audio=True,
                input_field="images",
            ),
            rendering=PromptRendering(
                prefix="制作一段可验收的短剧镜头。",
                transport_field="images",
            ),
        ),
        "grok-imagine-video": ModelProfile(
            profile_id="grok-imagine-video",
            model_id="grok-imagine-video",
            provider="xlinks",
            display_name="Grok Imagine Video",
            aliases=("xlinks-grok-video/grok-imagine-video#i2v",),
            capabilities=ModelCapabilities(
                modes=frozenset({"t2v", "i2v", "r2v"}),
                duration_min=1,
                duration_max=10,
                resolutions=frozenset({"480p", "720p", "1080p"}),
                default_resolution="720p",
                max_reference_images=9,
                supports_audio=False,
                input_field="images",
            ),
            rendering=PromptRendering(
                prefix="生成一段连贯的短剧视频镜头。",
                reference_instruction="第{order}张参考图（{alias}）仅承担{purpose}，保持其关键视觉特征。",
                transport_field="images",
            ),
        ),
    }
