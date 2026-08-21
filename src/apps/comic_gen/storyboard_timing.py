"""Utilities for keeping explicit screenplay timelines duration-safe.

The storyboard workbench treats every ``StoryboardFrame`` as one video
generation task.  A screenplay, however, may describe several editorial beats
inside a single short clip (for example ``0-1s``, ``1-2s`` ... inside a five
second sequence).  Turning every beat into a generation task inflates the final
runtime and does not match the clip/beat hierarchy used by mature editors.

This module recognizes explicit time ranges and collapses them into generation
clips of at most ``max_clip_seconds``.  The original beats stay available as
structured metadata and in the assembled action prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Optional


_TIMELINE_HEADER_RE = re.compile(
    r"(?:#{1,6}\s*)?\*{0,2}[【\[]\s*"
    r"(?P<start>\d+(?:\.\d+)?)\s*[—–－~～\-]\s*"
    r"(?P<end>\d+(?:\.\d+)?)\s*(?:秒|s)\s*"
    r"(?:[｜|]\s*(?P<label>[^】\]]+?))?\s*[】\]]\*{0,2}",
    re.IGNORECASE | re.MULTILINE,
)

_MARKDOWN_HEADING_RE = re.compile(r"(?:^|\s)#{1,6}\s*", re.MULTILINE)
_MARKDOWN_RULE_RE = re.compile(r"\s*-{3,}\s*")
_WHITESPACE_RE = re.compile(r"\s+")
_TARGET_DURATION_RE = re.compile(
    r"(?:目标时长|总时长|全片时长|视频时长|片长|总共(?:约|才)?)\s*[：:=]?\s*"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*分(?:钟)?)?\s*"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*秒)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TimelineBeat:
    """One explicitly timed editorial beat in the source screenplay."""

    start_seconds: float
    end_seconds: float
    label: Optional[str]
    description: str

    @property
    def duration(self) -> float:
        return self.end_seconds - self.start_seconds


@dataclass(frozen=True)
class TimelineParseResult:
    """Parsed beats plus screenplay context that precedes the first beat."""

    preamble: str
    beats: tuple[TimelineBeat, ...]


def _clean_screenplay_text(value: str) -> str:
    value = _MARKDOWN_RULE_RE.sub("\n", value)
    value = _MARKDOWN_HEADING_RE.sub("\n", value)
    value = value.replace("**", "").replace("__", "")
    return _WHITESPACE_RE.sub(" ", value).strip(" \n:-")


def _seconds_label(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def parse_explicit_timeline(text: str) -> Optional[TimelineParseResult]:
    """Parse ``【0-1秒｜标题】``-style blocks from a screenplay.

    Returns ``None`` when fewer than one valid, increasing time range is
    present.  Invalid/overlapping ranges are ignored so ordinary bracketed
    prose cannot accidentally switch the generation strategy.
    """

    matches = list(_TIMELINE_HEADER_RE.finditer(text or ""))
    if not matches:
        return None

    beats: list[TimelineBeat] = []
    previous_end: Optional[float] = None
    for index, match in enumerate(matches):
        start = float(match.group("start"))
        end = float(match.group("end"))
        if end <= start:
            continue
        if previous_end is not None and start < previous_end - 1e-9:
            continue

        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        description = _clean_screenplay_text(text[match.end():body_end])
        if not description:
            continue

        label = _clean_screenplay_text(match.group("label") or "") or None
        beats.append(
            TimelineBeat(
                start_seconds=start,
                end_seconds=end,
                label=label,
                description=description,
            )
        )
        previous_end = end

    if not beats:
        return None

    preamble = _clean_screenplay_text(text[: matches[0].start()])
    return TimelineParseResult(preamble=preamble, beats=tuple(beats))


def extract_target_duration_seconds(text: str) -> Optional[float]:
    """Read an explicitly stated total runtime from screenplay prose.

    The short-drama workflow needs an authoritative episode/clip budget even
    when the author did not annotate every beat with ``【0-4秒】`` headers.
    Only labelled totals are accepted; arbitrary numbers followed by ``秒``
    are deliberately ignored so dialogue and camera notes cannot become the
    production duration by accident.
    """

    for match in _TARGET_DURATION_RE.finditer(text or ""):
        minutes_raw = match.group("minutes")
        seconds_raw = match.group("seconds")
        if minutes_raw is None and seconds_raw is None:
            continue
        total = (float(minutes_raw or 0) * 60.0) + float(seconds_raw or 0)
        if total > 0:
            return total
    return None


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _first_nonempty(
    frames: list[dict[str, Any]],
    key: str,
    default: Any = None,
) -> Any:
    for frame in frames:
        value = frame.get(key)
        if value not in (None, "", []):
            return value
    return default


def _group_beats(
    beats: tuple[TimelineBeat, ...], max_clip_seconds: float
) -> list[list[TimelineBeat]]:
    groups: list[list[TimelineBeat]] = []
    current: list[TimelineBeat] = []

    normalized_beats: list[TimelineBeat] = []
    for beat in beats:
        cursor = beat.start_seconds
        while beat.end_seconds - cursor > max_clip_seconds:
            normalized_beats.append(
                TimelineBeat(
                    start_seconds=cursor,
                    end_seconds=cursor + max_clip_seconds,
                    label=beat.label,
                    description=beat.description,
                )
            )
            cursor += max_clip_seconds
        if beat.end_seconds > cursor:
            normalized_beats.append(
                TimelineBeat(
                    start_seconds=cursor,
                    end_seconds=beat.end_seconds,
                    label=beat.label,
                    description=beat.description,
                )
            )

    for beat in normalized_beats:
        if current and beat.end_seconds - current[0].start_seconds > max_clip_seconds:
            groups.append(current)
            current = []
        current.append(beat)

    if current:
        groups.append(current)
    return groups


def _raw_frame_chunk(
    raw_frames: list[dict[str, Any]],
    group: list[TimelineBeat],
    first_start: float,
    last_end: float,
) -> list[dict[str, Any]]:
    """Assign consecutive LLM frames to a timed generation clip.

    The LLM may still return one frame per visual action.  Proportional,
    monotonic slicing lets us retain its entity/camera hints while the source
    timeline remains the authority for clip count and duration.
    """

    if not raw_frames:
        return []
    span = max(last_end - first_start, 1e-9)
    start_ratio = (group[0].start_seconds - first_start) / span
    end_ratio = (group[-1].end_seconds - first_start) / span
    start_index = min(
        len(raw_frames) - 1,
        max(0, math.floor(start_ratio * len(raw_frames))),
    )
    end_index = min(
        len(raw_frames),
        max(start_index + 1, math.ceil(end_ratio * len(raw_frames))),
    )
    return raw_frames[start_index:end_index]


def _frame_duration(frame: dict[str, Any]) -> float:
    try:
        value = float(frame.get("duration") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 5.0


def _frame_description(frame: dict[str, Any]) -> str:
    return _clean_screenplay_text(
        str(
            frame.get("action_summary")
            or frame.get("action_description")
            or frame.get("visual_description")
            or ""
        )
    )


def group_storyboard_frames_into_clips(
    raw_frames: list[dict[str, Any]],
    *,
    max_clip_seconds: float = 15.0,
    target_duration_seconds: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Group ordinary LLM storyboard rows into generation clips.

    Xiaoyunque-style planning has two levels: a generated clip (normally at
    most 15 seconds) and multiple editorial beats inside it.  LLM output often
    arrives as one row per visual action, so treating every row as a remote
    video task produces too many tasks and inflates runtime.  This function
    converts those rows into duration-bounded clips while retaining each row
    as a structured ``timeline_beats`` entry.

    When the screenplay declares a total runtime, the raw LLM durations are
    proportionally normalized to that budget before grouping.  This makes a
    "5 秒" brief stay five seconds even if the model returned many actions.
    """

    frames = [frame for frame in raw_frames if isinstance(frame, dict)]
    if not frames:
        return []
    if max_clip_seconds <= 0:
        raise ValueError("max_clip_seconds must be greater than zero")

    durations = [_frame_duration(frame) for frame in frames]
    raw_total = sum(durations)
    if target_duration_seconds is not None and target_duration_seconds > 0 and raw_total > 0:
        scale = target_duration_seconds / raw_total
        durations = [duration * scale for duration in durations]

    # Split a single overlong action at the clip boundary.  The repeated
    # description is intentional: each generated clip must remain standalone.
    normalized: list[tuple[dict[str, Any], TimelineBeat]] = []
    cursor = 0.0
    for index, (frame, duration) in enumerate(zip(frames, durations), start=1):
        remaining = duration
        description = _frame_description(frame) or f"分镜 {index}"
        label = _clean_screenplay_text(str(frame.get("label") or "")) or f"分镜{index}"
        while remaining > max_clip_seconds + 1e-9:
            normalized.append(
                (
                    frame,
                    TimelineBeat(cursor, cursor + max_clip_seconds, label, description),
                )
            )
            cursor += max_clip_seconds
            remaining -= max_clip_seconds
        if remaining > 1e-9:
            normalized.append(
                (frame, TimelineBeat(cursor, cursor + remaining, label, description))
            )
            cursor += remaining

    # Avoid floating-point drift leaking into persisted source timelines
    # (e.g. 4.999999999999999 for a declared five-second clip).
    if target_duration_seconds is not None and normalized:
        last_frame, last_beat = normalized[-1]
        if target_duration_seconds > last_beat.start_seconds:
            normalized[-1] = (
                last_frame,
                TimelineBeat(
                    last_beat.start_seconds,
                    float(target_duration_seconds),
                    last_beat.label,
                    last_beat.description,
                ),
            )

    groups: list[list[tuple[dict[str, Any], TimelineBeat]]] = []
    current: list[tuple[dict[str, Any], TimelineBeat]] = []
    for item in normalized:
        beat = item[1]
        if current and beat.end_seconds - current[0][1].start_seconds > max_clip_seconds + 1e-9:
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)

    clips: list[dict[str, Any]] = []
    for group in groups:
        source_frames = [item[0] for item in group]
        beats = [item[1] for item in group]
        beat_lines: list[str] = []
        for beat_index, (frame, beat) in enumerate(group, start=1):
            scene_name = _clean_screenplay_text(str(frame.get("scene_ref_name") or ""))
            scene_prefix = f"【场景：{scene_name}】" if scene_name else ""
            beat_lines.append(
                f"分镜{beat_index} "
                f"{_seconds_label(beat.start_seconds)}-{_seconds_label(beat.end_seconds)}秒："
                f"{scene_prefix}{beat.description}"
            )

        character_names = _unique_strings(
            name for frame in source_frames for name in (frame.get("character_ref_names") or [])
        )
        prop_names = _unique_strings(
            name for frame in source_frames for name in (frame.get("prop_ref_names") or [])
        )
        dialogue_values = _unique_strings(frame.get("dialogue") for frame in source_frames)
        speaker_values = _unique_strings(frame.get("speaker") for frame in source_frames)
        duration = beats[-1].end_seconds - beats[0].start_seconds
        clips.append(
            {
                "scene_ref_name": _first_nonempty(source_frames, "scene_ref_name", ""),
                "character_ref_names": character_names,
                "prop_ref_names": prop_names,
                "action_summary": "\n".join(beat_lines),
                "visual_atmosphere": _first_nonempty(source_frames, "visual_atmosphere"),
                "shot_size": _first_nonempty(source_frames, "shot_size", "中景"),
                "camera_angle": _first_nonempty(source_frames, "camera_angle", "平视"),
                "camera_movement": _first_nonempty(source_frames, "camera_movement", "静止"),
                "dialogue": " / ".join(dialogue_values) if dialogue_values else None,
                "speaker": speaker_values[0] if len(speaker_values) == 1 else None,
                "duration": max(1, int(round(duration))),
                "timeline_start_seconds": beats[0].start_seconds,
                "timeline_end_seconds": beats[-1].end_seconds,
                "timeline_beats": [
                    {
                        "start_seconds": beat.start_seconds,
                        "end_seconds": beat.end_seconds,
                        "label": beat.label,
                        "description": beat.description,
                    }
                    for beat in beats
                ],
            }
        )
    return clips


def collapse_timed_storyboard_frames(
    text: str,
    raw_frames: list[dict[str, Any]],
    *,
    max_clip_seconds: float = 15.0,
) -> Optional[list[dict[str, Any]]]:
    """Collapse explicit screenplay beats into duration-safe video clips.

    ``None`` means the screenplay has no explicit timeline and the caller
    should keep the normal one-action-per-frame behavior.
    """

    parsed = parse_explicit_timeline(text)
    if parsed is None:
        return None
    if max_clip_seconds <= 0:
        raise ValueError("max_clip_seconds must be greater than zero")

    groups = _group_beats(parsed.beats, max_clip_seconds)
    first_start = parsed.beats[0].start_seconds
    last_end = parsed.beats[-1].end_seconds
    clips: list[dict[str, Any]] = []

    for group in groups:
        source_frames = _raw_frame_chunk(raw_frames, group, first_start, last_end)
        beat_lines = []
        for beat in group:
            title = f"（{beat.label}）" if beat.label else ""
            beat_lines.append(
                f"{_seconds_label(beat.start_seconds)}-{_seconds_label(beat.end_seconds)}秒{title}："
                f"{beat.description}"
            )

        action_parts = []
        if parsed.preamble:
            action_parts.append(f"全局设定：{parsed.preamble}")
        action_parts.extend(beat_lines)

        character_names = _unique_strings(
            name
            for frame in source_frames
            for name in (frame.get("character_ref_names") or [])
        )
        prop_names = _unique_strings(
            name
            for frame in source_frames
            for name in (frame.get("prop_ref_names") or [])
        )
        dialogue_values = _unique_strings(
            frame.get("dialogue") for frame in source_frames
        )
        speaker_values = _unique_strings(
            frame.get("speaker") for frame in source_frames
        )

        duration = group[-1].end_seconds - group[0].start_seconds
        rounded_duration = max(1, int(round(duration)))
        clips.append(
            {
                "scene_ref_name": _first_nonempty(source_frames, "scene_ref_name", ""),
                "character_ref_names": character_names,
                "prop_ref_names": prop_names,
                "action_summary": "\n".join(action_parts),
                "visual_atmosphere": _first_nonempty(source_frames, "visual_atmosphere"),
                "shot_size": _first_nonempty(source_frames, "shot_size", "中景"),
                "camera_angle": _first_nonempty(source_frames, "camera_angle", "平视"),
                "camera_movement": _first_nonempty(source_frames, "camera_movement", "静止"),
                "dialogue": " / ".join(dialogue_values) if dialogue_values else None,
                "speaker": speaker_values[0] if len(speaker_values) == 1 else None,
                "duration": rounded_duration,
                "timeline_start_seconds": group[0].start_seconds,
                "timeline_end_seconds": group[-1].end_seconds,
                "timeline_beats": [
                    {
                        "start_seconds": beat.start_seconds,
                        "end_seconds": beat.end_seconds,
                        "label": beat.label,
                        "description": beat.description,
                    }
                    for beat in group
                ],
            }
        )

    return clips
