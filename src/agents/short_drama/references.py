"""Normalize Skill/editor references and bind them to authorized media IDs."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from ..core.contracts import (
    AssetReference,
    AuthorizationStatus,
    FindingSeverity,
    ReferenceType,
    make_finding,
)

GENERIC_REFERENCE_RE = re.compile(r"@(?P<kind>图片|图|视频|音频|文本)\s*(?P<slot>[A-Za-z0-9_-]+)")
LEGACY_REFERENCE_RE = re.compile(r"\[(?P<kind>character|scene|prop)(?P<slot>\d*)\s*:\s*(?P<name>[^\]]+)\]")


class ParsedReference:
    def __init__(self, alias: str, type: ReferenceType, purpose: str, raw: str, order: int):
        self.alias, self.type, self.purpose, self.raw, self.order = alias, type, purpose, raw, order

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "type": self.type.value,
            "purpose": self.purpose,
            "raw": self.raw,
            "order": self.order,
        }


def _generic_type(kind: str) -> ReferenceType:
    return {
        "图片": ReferenceType.IMAGE,
        "图": ReferenceType.IMAGE,
        "视频": ReferenceType.VIDEO,
        "音频": ReferenceType.AUDIO,
        "文本": ReferenceType.TEXT,
    }[kind]


def parse_references(text: str) -> list[ParsedReference]:
    """Return deterministic, first-seen references from generic and legacy syntax."""
    if not isinstance(text, str):
        raise TypeError("引用文本必须是字符串")
    parsed: list[ParsedReference] = []
    by_identity: dict[str, ParsedReference] = {}
    matches = [
        (match.start(), match, "generic") for match in GENERIC_REFERENCE_RE.finditer(text)
    ] + [
        (match.start(), match, "legacy") for match in LEGACY_REFERENCE_RE.finditer(text)
    ]
    for _, match, syntax in sorted(matches, key=lambda item: item[0]):
        if syntax == "generic":
            kind = match.group("kind")
            slot = match.group("slot")
            ref_type = _generic_type(kind)
            alias = f"{ref_type.value}:{slot}"
            purpose = {ReferenceType.IMAGE: "视觉素材", ReferenceType.VIDEO: "动作与节奏参考", ReferenceType.AUDIO: "声音参考", ReferenceType.TEXT: "文本参考"}[ref_type]
        else:
            kind = match.group("kind")
            slot = match.group("slot")
            name = match.group("name").strip()
            ref_type = {"character": ReferenceType.IMAGE, "scene": ReferenceType.IMAGE, "prop": ReferenceType.IMAGE}[kind]
            alias = f"{kind}{slot}:{name}"
            purpose = {"character": "角色外观", "scene": "场景外观", "prop": "道具外观"}[kind]
        identity = alias.lower()
        existing = by_identity.get(identity)
        if existing:
            # Same identity in two syntaxes is fine; conflicting slots remain
            # visible through the stable first order.
            continue
        item = ParsedReference(alias, ref_type, purpose, match.group(0), len(parsed) + 1)
        parsed.append(item)
        by_identity[identity] = item
    return parsed


def normalize_references(text: str) -> tuple[list[ParsedReference], list[Any]]:
    """Normalize references and report conflicting positional aliases."""
    parsed = parse_references(text)
    findings: list[Any] = []
    slots: dict[str, str] = {}
    for item in parsed:
        if ":" not in item.alias:
            continue
        kind, slot = item.alias.split(":", 1)[0], item.alias.split(":", 1)[1]
        if kind in {"image", "video", "audio", "text"}:
            key = slot
        else:
            match = re.match(r"(?:character|scene|prop)(\d+)$", kind)
            key = match.group(1) if match else ""
        if not key:
            continue
        previous = slots.get(key)
        if previous and previous != item.alias:
            findings.append(
                make_finding(
                    FindingSeverity.WARNING,
                    "reference_slot_conflict",
                    f"素材槽位 {key} 同时对应 {previous} 和 {item.alias}",
                    "reference-normalizer",
                    path=item.raw,
                )
            )
        slots[key] = item.alias
    return parsed, findings


def _lookup(materials: Mapping[str, Any], parsed: ParsedReference) -> Any:
    candidates = [parsed.alias, parsed.alias.lower(), parsed.raw]
    if ":" in parsed.alias:
        prefix, name = parsed.alias.split(":", 1)
        candidates.extend((name, name.lower(), prefix))
    for key in candidates:
        if key in materials:
            return materials[key]
    # Generic image:1 may be supplied as 图片1 or image1.
    if parsed.alias.startswith("image:"):
        slot = parsed.alias.split(":", 1)[1]
        for key in (f"图片{slot}", f"图{slot}", f"image{slot}", slot):
            if key in materials:
                return materials[key]
    return None


def bind_references(
    parsed: list[ParsedReference] | str,
    materials: Mapping[str, Any],
    *,
    source: str = "application_asset",
) -> tuple[list[AssetReference], list[Any]]:
    """Bind references without accepting guessed URLs or untrusted paths."""
    if isinstance(parsed, str):
        refs, normalization_findings = normalize_references(parsed)
    else:
        refs, normalization_findings = parsed, []
    bindings: list[AssetReference] = []
    findings: list[Any] = list(normalization_findings)
    for item in refs:
        value = _lookup(materials, item)
        media_id: str | None = None
        resource_id: str | None = None
        authorized = AuthorizationStatus.UNKNOWN
        metadata: dict[str, Any] = {}
        if isinstance(value, AssetReference):
            media_id, resource_id, authorized, metadata = value.media_id, value.resource_id, value.authorization, dict(value.metadata)
            purpose = value.purpose or item.purpose
        elif isinstance(value, Mapping):
            media_id = _safe_id(value.get("media_id"))
            resource_id = _safe_id(value.get("resource_id"))
            authorized = _authorization(value.get("authorization", value.get("authorized", False)))
            metadata = {key: value[key] for key in ("role", "asset_id", "name") if key in value}
            purpose = str(value.get("purpose") or value.get("role") or item.purpose)
        else:
            media_id = _safe_id(value)
            purpose = item.purpose
            if media_id:
                authorized = AuthorizationStatus.AUTHORIZED
        if not media_id and not resource_id:
            authorized = AuthorizationStatus.UNAUTHORIZED
        ref = AssetReference(
            alias=item.alias,
            type=item.type,
            purpose=purpose,
            source=source,
            media_id=media_id,
            resource_id=resource_id,
            authorization=authorized,
            order=item.order,
            semantic_role=metadata.get("role") or purpose,
            metadata=metadata,
        )
        bindings.append(ref)
        if authorized is not AuthorizationStatus.AUTHORIZED or not media_id:
            findings.append(
                make_finding(
                    FindingSeverity.BLOCKING,
                    "reference_unresolved",
                    f"引用 {item.raw} 未绑定已授权媒体 ID",
                    "asset-binding",
                    path=item.raw,
                )
            )
    return bindings, findings


def resolve_authorized_media(
    references: list[AssetReference],
    resolver: Callable[[str], Any],
) -> tuple[list[AssetReference], list[Any]]:
    """Resolve application IDs through a scoped media service.

    ``resolver`` may return a media record/dict or ``None``. URLs are never
    accepted as a substitute for an application media ID.
    """
    findings: list[Any] = []
    result: list[AssetReference] = []
    for reference in references:
        if not reference.media_id:
            result.append(reference)
            continue
        record = resolver(reference.media_id)
        authorized = bool(record)
        if isinstance(record, Mapping):
            authorized = authorized and bool(record.get("authorized", record.get("lifecycle_state", "active") == "active"))
        result.append(reference.model_copy(update={"authorization": AuthorizationStatus.AUTHORIZED if authorized else AuthorizationStatus.UNAUTHORIZED}))
        if not authorized:
            findings.append(
                make_finding(
                    FindingSeverity.BLOCKING,
                    "media_not_authorized",
                    f"媒体 {reference.media_id} 不属于当前工作区或未授权",
                    "media-permission",
                    path=reference.alias,
                )
            )
    return result, findings


def _safe_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text.startswith(("http://", "https://", "data:", "/")):
        return None
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", text) else None


def _authorization(value: Any) -> AuthorizationStatus:
    if isinstance(value, bool):
        return AuthorizationStatus.AUTHORIZED if value else AuthorizationStatus.UNAUTHORIZED
    try:
        return AuthorizationStatus(str(value).lower())
    except ValueError:
        return AuthorizationStatus.UNKNOWN
