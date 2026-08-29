"""Compile a provider-neutral shot into a model-profile payload."""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ..core.contracts import (
    AssetReference,
    AuthorizationStatus,
    FindingSeverity,
    ReferenceType,
    ShotPackage,
    ValidationFinding,
    make_finding,
)
from ..core.errors import AgentBlockedError
from .profiles import ModelProfile, ProfileResolver


class CompiledReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order: int = Field(ge=1)
    alias: str
    type: ReferenceType
    purpose: str
    semantic_role: str | None = None
    media_id: str


class CompiledShot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    shot_id: str
    profile_id: str
    model_id: str
    provider: str
    generation_mode: str
    prompt: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    input_media_ids: list[str] = Field(default_factory=list)
    reference_map: list[CompiledReference] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(item.severity is FindingSeverity.BLOCKING for item in self.findings)

    def gateway_content(self) -> dict[str, Any]:
        """Return content for Cloud AI Gateway without leaking URLs."""
        return {
            "operation": "short_drama.video",
            "shot_id": self.shot_id,
            "prompt": self.prompt,
            "reference_map": [item.model_dump(mode="json") for item in self.reference_map],
        }

    def gateway_parameters(self) -> dict[str, Any]:
        # The Gateway chooses the model from its server-side capability route;
        # provider model IDs are retained in the run snapshot, never sent as a
        # client override.
        return {**self.parameters, "generation_mode": self.generation_mode}


class PromptCompiler:
    def __init__(self, resolver: ProfileResolver | None = None):
        self.resolver = resolver or ProfileResolver()

    def compile(
        self,
        shot: ShotPackage,
        *,
        profile: str | ModelProfile | None = None,
        bound_references: list[AssetReference] | None = None,
        parameters: Mapping[str, Any] | None = None,
        require_audio: bool = False,
        audio_policy: str = "warning",
    ) -> CompiledShot:
        selected = profile if isinstance(profile, ModelProfile) else self.resolver.resolve(
            profile, generation_mode=shot.generation_mode, parameters=parameters
        )
        requested = dict(parameters or {})
        findings: list[ValidationFinding] = []
        capabilities = selected.capabilities

        if shot.duration < capabilities.duration_min or shot.duration > capabilities.duration_max:
            findings.append(
                make_finding(
                    FindingSeverity.BLOCKING,
                    "duration_out_of_range",
                    f"时长 {shot.duration:g}s 超出 {selected.display_name} 的范围 "
                    f"{capabilities.duration_min:g}-{capabilities.duration_max:g}s",
                    "profile-compiler",
                    shot_id=shot.shot_id,
                )
            )
        resolution = requested.get("resolution")
        if resolution is not None and capabilities.resolutions and str(resolution) not in capabilities.resolutions:
            findings.append(
                make_finding(
                    FindingSeverity.BLOCKING,
                    "resolution_unsupported",
                    f"分辨率 {resolution} 不在模型允许范围内",
                    "profile-compiler",
                    shot_id=shot.shot_id,
                )
            )

        references = list(bound_references or [])
        if len(references) > capabilities.max_reference_images:
            findings.append(
                make_finding(
                    FindingSeverity.BLOCKING,
                    "reference_limit_exceeded",
                    f"参考素材 {len(references)} 张，超过模型上限 {capabilities.max_reference_images} 张",
                    "profile-compiler",
                    shot_id=shot.shot_id,
                )
            )
        ordered: list[CompiledReference] = []
        for index, reference in enumerate(sorted(references, key=lambda item: (item.order, item.alias)), start=1):
            if reference.authorization is not AuthorizationStatus.AUTHORIZED or not reference.media_id:
                findings.append(
                    make_finding(
                        FindingSeverity.BLOCKING,
                        "reference_not_authorized",
                        f"素材引用 {reference.alias} 未绑定已授权 media_id",
                        "profile-compiler",
                        shot_id=shot.shot_id,
                    )
                )
                continue
            ordered.append(
                CompiledReference(
                    order=index,
                    alias=reference.alias,
                    type=reference.type,
                    purpose=reference.purpose,
                    semantic_role=reference.semantic_role,
                    media_id=reference.media_id,
                )
            )

        if require_audio and not capabilities.supports_audio:
            severity = FindingSeverity.BLOCKING if audio_policy == "blocking" else FindingSeverity.WARNING
            findings.append(
                make_finding(
                    severity,
                    "audio_unsupported",
                    "当前模型不支持模型内音频，将由后期音频链路处理" if severity is FindingSeverity.WARNING else "当前模型不支持请求的模型内音频",
                    "profile-compiler",
                    shot_id=shot.shot_id,
                )
            )
            requested.pop("audio", None)
        elif not capabilities.supports_audio:
            requested.pop("audio", None)

        prompt = self._render_prompt(shot, selected, ordered)
        requested.setdefault("duration", shot.duration)
        requested.setdefault("resolution", capabilities.default_resolution)
        # Provider parameters are profile-owned; client-only model selectors and
        # raw URLs are intentionally omitted.
        requested.pop("model_choice", None)
        for key in tuple(requested):
            if key.lower().endswith("url") or key.lower() in {"image_url", "images", "ref_image_urls"}:
                requested.pop(key, None)

        return CompiledShot(
            shot_id=shot.shot_id,
            profile_id=selected.profile_id,
            model_id=selected.model_id,
            provider=selected.provider,
            generation_mode=shot.generation_mode,
            prompt=prompt,
            parameters=requested,
            input_media_ids=[reference.media_id for reference in ordered],
            reference_map=ordered,
            findings=findings,
        )

    @staticmethod
    def _render_prompt(shot: ShotPackage, profile: ModelProfile, references: list[CompiledReference]) -> str:
        sections = [
            profile.rendering.prefix,
            f"镜头编号：{shot.shot_id}；时长：{shot.duration:g}s；画幅：{shot.aspect_ratio}。",
        ]
        for label, value in (
            ("镜头目的", shot.purpose),
            ("主体与场景", "；".join(filter(None, (shot.subject, shot.scene)))),
            ("起始状态", shot.start_state),
            ("动作", "；".join(shot.actions)),
            ("镜头", shot.camera),
            ("结束状态", shot.end_state),
            ("对白", shot.dialogue),
            ("音频", shot.audio),
            ("视觉风格", shot.style),
        ):
            if value:
                sections.append(f"{label}：{value}")
        if shot.timeline:
            sections.append(
                "时间轴：" + "；".join(
                    f"{segment.start:g}-{segment.end:g}s {segment.action}" for segment in shot.timeline
                )
            )
        if references:
            sections.append(
                "素材职责：" + "；".join(
                    profile.rendering.reference_instruction.format(
                        order=reference.order,
                        alias=reference.alias,
                        purpose=reference.purpose,
                    )
                    for reference in references
                )
            )
        sections.append("约束：保持人物、服装、道具、视线和镜头轴线连续；不要新增未授权素材或台词。")
        if profile.rendering.suffix:
            sections.append(profile.rendering.suffix)
        return "\n".join(item for item in sections if item.strip())


def ensure_submit_ready(compiled: CompiledShot) -> None:
    if compiled.blocking:
        raise AgentBlockedError(
            f"镜头 {compiled.shot_id} 存在 {sum(item.severity is FindingSeverity.BLOCKING for item in compiled.findings)} 个阻断项",
            code="PROMPT_COMPILE_BLOCKED",
        )


def build_gateway_request(
    compiled: CompiledShot,
    *,
    project_id: str | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Build the Cloud AI Gateway contract for an internal task tool."""
    return {
        "capability": f"video.{compiled.generation_mode}",
        "idempotency_key": idempotency_key,
        "project_id": project_id,
        "media_ids": list(compiled.input_media_ids),
        "content": compiled.gateway_content(),
        "parameters": compiled.gateway_parameters(),
    }
