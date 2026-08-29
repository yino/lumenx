"""Deterministic short-drama quality gates."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from ..core.contracts import (
    AssetReference,
    AuthorizationStatus,
    FindingSeverity,
    ReferenceType,
    ShotPackage,
    ValidationFinding,
    ValidationReport,
    make_finding,
)


def validate_shot(
    shot: ShotPackage,
    *,
    references: Iterable[AssetReference] = (),
    profile: Any | None = None,
) -> ValidationReport:
    findings: list[ValidationFinding] = []
    refs = list(references)
    _check_timeline(shot, findings)
    _check_camera_conflicts(shot, findings)
    _check_references(shot, refs, findings)
    _check_safety(shot, findings)
    if profile is not None:
        modes = getattr(getattr(profile, "capabilities", None), "modes", ())
        if shot.generation_mode not in modes:
            findings.append(make_finding(FindingSeverity.BLOCKING, "mode_unsupported", f"模型不支持 {shot.generation_mode}", "profile-validator", shot_id=shot.shot_id))
    return ValidationReport(findings=findings)


def validate_package(
    shots: Iterable[ShotPackage],
    *,
    references: Iterable[AssetReference] = (),
    profile: Any | None = None,
) -> ValidationReport:
    findings: list[ValidationFinding] = []
    references_list = list(references)
    for shot in shots:
        findings.extend(validate_shot(shot, references=references_list, profile=profile).findings)
    _check_batch_duplicate_ids(list(shots), findings)
    return ValidationReport(findings=findings)


def _check_timeline(shot: ShotPackage, findings: list[ValidationFinding]) -> None:
    if not shot.timeline:
        findings.append(make_finding(FindingSeverity.WARNING, "missing_timeline", "未提供可验收时间轴", "timeline-validator", shot_id=shot.shot_id))
        return
    segments = sorted(shot.timeline, key=lambda item: item.start)
    if segments[0].start > 0.01:
        findings.append(make_finding(FindingSeverity.BLOCKING, "timeline_start", "时间轴必须从 0 秒开始", "timeline-validator", shot_id=shot.shot_id))
    for previous, current in zip(segments, segments[1:]):
        if current.start < previous.end - 0.01:
            findings.append(make_finding(FindingSeverity.BLOCKING, "timeline_overlap", "时间轴存在重叠", "timeline-validator", shot_id=shot.shot_id))
        elif current.start > previous.end + 0.01:
            findings.append(make_finding(FindingSeverity.BLOCKING, "timeline_gap", "时间轴存在空段", "timeline-validator", shot_id=shot.shot_id))
    if abs(segments[-1].end - shot.duration) > 0.01:
        findings.append(make_finding(FindingSeverity.BLOCKING, "timeline_duration", f"时间轴结束于 {segments[-1].end:g}s，与目标 {shot.duration:g}s 不一致", "timeline-validator", shot_id=shot.shot_id))


def _check_camera_conflicts(shot: ShotPackage, findings: list[ValidationFinding]) -> None:
    text = " ".join((shot.camera, *(segment.camera for segment in shot.timeline)))
    if "固定机位" in text and any(term in text for term in ("环绕", "跟拍", "推镜", "拉镜", "摇镜")):
        findings.append(make_finding(FindingSeverity.WARNING, "camera_conflict", "固定机位与移动运镜同时出现，请确认是否分属不同时间段", "camera-validator", shot_id=shot.shot_id))
    if "一镜到底" in text and any(term in text for term in ("快切", "切镜", "多次转场")):
        findings.append(make_finding(FindingSeverity.WARNING, "edit_conflict", "一镜到底与剪辑切换同时出现", "camera-validator", shot_id=shot.shot_id))


def _check_references(shot: ShotPackage, refs: list[AssetReference], findings: list[ValidationFinding]) -> None:
    expected = {reference.alias for reference in shot.references}
    provided = {reference.alias for reference in refs}
    for alias in sorted(expected - provided):
        findings.append(make_finding(FindingSeverity.BLOCKING, "reference_missing", f"镜头引用 {alias} 没有绑定记录", "reference-validator", shot_id=shot.shot_id))
    for reference in refs:
        if reference.alias not in expected and expected:
            continue
        if reference.authorization is not AuthorizationStatus.AUTHORIZED or not reference.media_id:
            findings.append(make_finding(FindingSeverity.BLOCKING, "reference_unauthorized", f"引用 {reference.alias} 未授权", "reference-validator", shot_id=shot.shot_id))
    aliases = [reference.alias for reference in shot.references]
    if len(aliases) != len(set(aliases)):
        findings.append(make_finding(FindingSeverity.WARNING, "reference_duplicate", "镜头包含重复素材引用", "reference-validator", shot_id=shot.shot_id))


def _check_safety(shot: ShotPackage, findings: list[ValidationFinding]) -> None:
    text = " ".join((shot.subject, shot.purpose, " ".join(shot.actions), shot.dialogue, str(shot.metadata.get("rights_note", ""))))
    if shot.metadata.get("real_person") and not shot.metadata.get("rights_confirmed"):
        findings.append(make_finding(FindingSeverity.BLOCKING, "real_person_rights", "真人素材缺少明确授权确认", "safety-validator", shot_id=shot.shot_id))
    if any(term in text for term in ("自残", "未成年人裸露", "炸弹制作", "毒品制作")):
        findings.append(make_finding(FindingSeverity.BLOCKING, "prohibited_content", "内容触发平台安全审核，需要人工处理", "safety-validator", shot_id=shot.shot_id))
    elif any(term in text for term in ("持刀", "枪", "高处跳", "危险驾驶")):
        findings.append(make_finding(FindingSeverity.WARNING, "dangerous_action", "危险动作需要安全审查和替代表达", "safety-validator", shot_id=shot.shot_id))


def _check_batch_duplicate_ids(shots: list[ShotPackage], findings: list[ValidationFinding]) -> None:
    ids = [shot.shot_id for shot in shots]
    if len(ids) != len(set(ids)):
        findings.append(make_finding(FindingSeverity.BLOCKING, "duplicate_shot_id", "生产包包含重复镜头 ID", "package-validator"))


def validate_prompt(text: str, duration: float | None = None) -> list[ValidationFinding]:
    """Compatibility entry point for the legacy ``validate_prompt.py`` script."""
    if not text.strip():
        return [make_finding(FindingSeverity.BLOCKING, "empty_prompt", "提示词为空", "prompt-validator")]
    findings: list[ValidationFinding] = []
    # Parse both Chinese and ASCII ranges used by existing Skill prompts.
    ranges = []
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|至)\s*(\d+(?:\.\d+)?)\s*(?:s|秒)", re.I)
    for match in pattern.finditer(text):
        start, end = float(match.group(1)), float(match.group(2))
        if end <= start:
            findings.append(make_finding(FindingSeverity.BLOCKING, "invalid_time_range", f"时间段 {match.group(0)} 无效", "prompt-validator"))
        ranges.append((start, end))
    if duration is not None and ranges and abs(max(end for _, end in ranges) - duration) > 0.01:
        findings.append(make_finding(FindingSeverity.BLOCKING, "timeline_duration", "提示词时间轴与目标时长不一致", "prompt-validator"))
    if not ranges:
        findings.append(make_finding(FindingSeverity.WARNING, "missing_timeline", "未发现可解析的时间段", "prompt-validator"))
    if "固定机位" in text and any(term in text for term in ("环绕", "跟拍", "推镜")):
        findings.append(make_finding(FindingSeverity.WARNING, "camera_conflict", "固定机位与移动运镜冲突", "prompt-validator"))
    return findings
