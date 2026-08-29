#!/usr/bin/env python3
"""Static checks for Seedance short-drama prompts.

The checker intentionally validates only observable structure. It does not
claim that a prompt will produce a good video, and it does not validate any
provider-specific upload limits unless the caller supplies them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


REFERENCE_RE = re.compile(r"@(?:图片|图|视频|音频|文本)\s*([A-Za-z0-9_-]+)?")
TIME_RE = re.compile(
    r"(?P<start>\d+(?:\.\d+)?)\s*(?:-|–|—|至)\s*"
    r"(?P<end>\d+(?:\.\d+)?)\s*(?:s|秒)",
    re.IGNORECASE,
)


@dataclass
class Finding:
    level: str
    code: str
    message: str


def add(findings: list[Finding], level: str, code: str, message: str) -> None:
    findings.append(Finding(level, code, message))


def check_references(text: str, findings: list[Finding]) -> None:
    for match in REFERENCE_RE.finditer(text):
        token = match.group(0)
        suffix = match.group(1)
        if not suffix:
            add(findings, "error", "ambiguous-reference", f"引用 {token} 缺少素材编号")
            continue
        if suffix in {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
            continue
        if suffix.isdigit():
            add(findings, "warning", "unusual-reference", f"引用 {token} 的编号超出常用范围，请确认素材清单")
        else:
            add(findings, "warning", "named-reference", f"引用 {token} 使用了非数字编号，请确认与素材表一致")

def check_timeline(text: str, duration: float | None, findings: list[Finding]) -> None:
    segments = []
    seen_ranges: set[tuple[float, float]] = set()
    for match in TIME_RE.finditer(text):
        start = float(match.group("start"))
        end = float(match.group("end"))
        if end <= start:
            add(findings, "error", "invalid-time-range", f"时间段 {match.group(0)} 的结束时间不大于开始时间")
        else:
            range_key = (start, end)
            # The standard deliverable repeats the timeline inside the
            # copyable prompt block. Identical ranges are one logical segment.
            if range_key not in seen_ranges:
                segments.append((start, end, match.group(0)))
                seen_ranges.add(range_key)

    if not segments:
        add(findings, "warning", "missing-timeline", "未发现可解析的时间段；超过约 8 秒的镜头建议补充时间轴")
        return

    segments.sort()
    for previous, current in zip(segments, segments[1:]):
        if current[0] < previous[1] - 0.01:
            add(findings, "error", "timeline-overlap", f"时间段 {previous[2]} 与 {current[2]} 重叠")
        elif current[0] > previous[1] + 0.01:
            add(findings, "error", "timeline-gap", f"时间段 {previous[2]} 与 {current[2]} 之间存在空段")

    if duration is not None:
        first_start, last_end = segments[0][0], segments[-1][1]
        if first_start > 0.01:
            add(findings, "error", "timeline-start", f"时间轴从 {first_start:g}s 开始，而目标时长从 0s 开始")
        if abs(last_end - duration) > 0.01:
            add(findings, "error", "timeline-duration", f"时间轴结束于 {last_end:g}s，与目标 {duration:g}s 不一致")


def check_conflicts(text: str, findings: list[Finding]) -> None:
    conflict_groups = [
        ("camera-conflict", ("固定机位",), ("环绕", "跟拍", "推镜", "拉镜", "摇镜"), "固定机位与移动运镜同时出现"),
        ("edit-conflict", ("一镜到底",), ("快切", "切镜", "多次转场"), "一镜到底与剪辑切换同时出现"),
    ]
    for code, left_terms, right_terms, message in conflict_groups:
        if any(term in text for term in left_terms) and any(term in text for term in right_terms):
            add(findings, "warning", code, message + "，请确认是否分属不同时间段")


def check_version_claims(text: str, findings: list[Finding]) -> None:
    claims = ("≤", "上限", "每张", "总文件数", "480p", "720p", "834×1112", "640×640")
    if any(claim in text for claim in claims) and not re.search(r"版本|入口|当前|官方|待确认", text):
        add(findings, "warning", "unscoped-platform-claim", "提示词含平台硬参数，但没有版本/入口/来源说明")


def validate(text: str, duration: float | None) -> list[Finding]:
    findings: list[Finding] = []
    if not text.strip():
        add(findings, "error", "empty-prompt", "提示词为空")
        return findings
    check_references(text, findings)
    check_timeline(text, duration, findings)
    check_conflicts(text, findings)
    check_version_claims(text, findings)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Seedance short-drama prompt")
    parser.add_argument("prompt", type=Path, help="Markdown or text file containing the prompt")
    parser.add_argument("--duration", type=float, help="Expected total duration in seconds")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON")
    args = parser.parse_args()

    try:
        text = args.prompt.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取失败: {exc}", file=sys.stderr)
        return 2

    findings = validate(text, args.duration)
    errors = sum(item.level == "error" for item in findings)
    warnings = sum(item.level == "warning" for item in findings)

    if args.as_json:
        print(json.dumps({"errors": errors, "warnings": warnings, "findings": [asdict(item) for item in findings]}, ensure_ascii=False, indent=2))
    else:
        for item in findings:
            print(f"[{item.level.upper()}] {item.code}: {item.message}")
        print(f"结果: {errors} 个错误, {warnings} 个警告")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
