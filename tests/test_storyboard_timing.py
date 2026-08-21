from __future__ import annotations

from src.apps.comic_gen.storyboard_timing import (
    collapse_timed_storyboard_frames,
    extract_target_duration_seconds,
    group_storyboard_frames_into_clips,
    parse_explicit_timeline,
)


FIVE_SECOND_SCRIPT = """**场景设定：** 明朝雨夜，青石长街，灯笼摇晃。

### 【0—1秒｜拔刀迎敌】
**镜头：低机位近景 → 快速推镜**
雨水砸落青石板。黑衣刺客凌空扑下，锦衣卫拔出绣春刀。

---

### 【1—2秒｜正面格挡】
双刀猛烈相撞，火星飞溅。

### 【2—3秒｜贴身反击】
锦衣卫贴身旋转，绣春刀横向掠过。

### 【3—4秒｜错身定格】
两人错身而过，雨水沿着刀锋滴落。

### 【4—5秒｜收刀绝杀】
锦衣卫收刀入鞘，刺客跪倒。
"""


def _raw_frame(index: int) -> dict:
    return {
        "scene_ref_name": "青石长街（雨夜）",
        "character_ref_names": ["锦衣卫", "黑衣刺客"],
        "prop_ref_names": ["绣春刀", "刺客长刀"],
        "action_summary": f"动作 {index}",
        "shot_size": "近景",
        "camera_angle": "仰视",
        "camera_movement": "快速推镜",
        "duration": 4,
    }


def test_parse_explicit_timeline_preserves_source_beats() -> None:
    parsed = parse_explicit_timeline(FIVE_SECOND_SCRIPT)

    assert parsed is not None
    assert len(parsed.beats) == 5
    assert parsed.beats[0].start_seconds == 0
    assert parsed.beats[-1].end_seconds == 5
    assert parsed.beats[0].label == "拔刀迎敌"
    assert "明朝雨夜" in parsed.preamble
    assert "黑衣刺客凌空扑下" in parsed.beats[0].description


def test_five_second_timeline_becomes_one_five_second_generation_clip() -> None:
    clips = collapse_timed_storyboard_frames(
        FIVE_SECOND_SCRIPT,
        [_raw_frame(index) for index in range(11)],
    )

    assert clips is not None
    assert len(clips) == 1
    clip = clips[0]
    assert clip["duration"] == 5
    assert clip["timeline_start_seconds"] == 0
    assert clip["timeline_end_seconds"] == 5
    assert len(clip["timeline_beats"]) == 5
    assert clip["character_ref_names"] == ["锦衣卫", "黑衣刺客"]
    assert "0-1秒（拔刀迎敌）" in clip["action_summary"]
    assert "4-5秒（收刀绝杀）" in clip["action_summary"]


def test_saved_single_line_script_still_preserves_timeline() -> None:
    single_line = " ".join(FIVE_SECOND_SCRIPT.splitlines())

    clips = collapse_timed_storyboard_frames(
        single_line,
        [_raw_frame(index) for index in range(11)],
    )

    assert clips is not None
    assert len(clips) == 1
    assert clips[0]["duration"] == 5
    assert len(clips[0]["timeline_beats"]) == 5


def test_explicit_timeline_splits_only_at_generation_clip_limit() -> None:
    script = """【0-8秒｜第一段】第一段动作。
【8-16秒｜第二段】第二段动作。"""

    clips = collapse_timed_storyboard_frames(
        script,
        [_raw_frame(1), _raw_frame(2)],
        max_clip_seconds=15,
    )

    assert clips is not None
    assert [clip["duration"] for clip in clips] == [8, 8]
    assert sum(clip["duration"] for clip in clips) == 16
    assert all(clip["duration"] <= 15 for clip in clips)


def test_single_long_timeline_beat_is_split_to_model_safe_clips() -> None:
    clips = collapse_timed_storyboard_frames(
        "【0-20秒｜长镜头】角色持续向前奔跑。",
        [_raw_frame(1)],
        max_clip_seconds=15,
    )

    assert clips is not None
    assert [clip["duration"] for clip in clips] == [15, 5]
    assert sum(clip["duration"] for clip in clips) == 20


def test_untimed_script_keeps_existing_storyboard_strategy() -> None:
    assert collapse_timed_storyboard_frames("人物走进房间。", [_raw_frame(1)]) is None


def test_target_duration_parser_supports_minutes_and_seconds() -> None:
    assert extract_target_duration_seconds("目标时长：3分48秒") == 228
    assert extract_target_duration_seconds("这个短片总共才5秒。") == 5
    assert extract_target_duration_seconds("角色停顿 5 秒后继续。") is None


def test_untimed_actions_are_nested_inside_duration_safe_clips() -> None:
    clips = group_storyboard_frames_into_clips(
        [_raw_frame(index) for index in range(6)],
        max_clip_seconds=15,
    )

    assert [clip["duration"] for clip in clips] == [12, 12]
    assert [len(clip["timeline_beats"]) for clip in clips] == [3, 3]
    assert all(clip["duration"] <= 15 for clip in clips)


def test_declared_five_second_budget_cannot_expand_to_many_tasks() -> None:
    clips = group_storyboard_frames_into_clips(
        [_raw_frame(index) for index in range(11)],
        max_clip_seconds=15,
        target_duration_seconds=5,
    )

    assert len(clips) == 1
    assert clips[0]["duration"] == 5
    assert len(clips[0]["timeline_beats"]) == 11
    assert clips[0]["timeline_end_seconds"] == 5


def test_thirty_second_planning_groups_more_beats_than_fifteen_seconds() -> None:
    frames = [_raw_frame(index) for index in range(6)]
    fifteen = group_storyboard_frames_into_clips(frames, max_clip_seconds=15)
    thirty = group_storyboard_frames_into_clips(frames, max_clip_seconds=30)

    assert len(fifteen) == 2
    assert len(thirty) == 1
    assert thirty[0]["duration"] == 24
