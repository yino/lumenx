from __future__ import annotations

import copy
import html
import re
import subprocess
import tempfile
import textwrap
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.apps.comic_gen.models import (
    Character,
    CustomVoice,
    GenerationStatus,
    ImageAsset,
    ImageVariant,
    Script,
    StoryboardFrame,
    VideoTask,
)
from src.apps.comic_gen.audio import (
    _compute_dialogue_hash,
    _effective_dialogue_text,
    _effective_instructions,
    dialogue_audio_is_stale,
)
from src.utils.system_check import get_ffmpeg_path

from .content_repositories import (
    OptimisticVersionConflictError,
    PostgresProjectRepository,
    ScopedDocumentNotFoundError,
)
from .asset_service import CloudAssetService
from .contracts import MediaWrite, VersionedDocument, WorkspaceContext
from .database import Database
from .media_storage import CloudMediaStorage, PostgresMediaRepository


_SRT_TIMESTAMP_PATTERN = "{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
_SUBTITLE_STYLE = (
    "FontName=Noto Sans CJK SC,FontSize=22,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=36"
)


def _clean_subtitle_text(value: str | None) -> str | None:
    if not value:
        return None
    without_tags = re.sub(r"<[^>]+>", "", html.unescape(value))
    normalized = re.sub(r"\s+", " ", without_tags).strip()
    return normalized or None


def _effective_dialogue(frame: StoryboardFrame) -> str | None:
    structured = frame.dialogue_structured
    line = _clean_subtitle_text(structured.line if structured else None)
    return line or _clean_subtitle_text(frame.dialogue)


def _effective_speaker(frame: StoryboardFrame) -> str | None:
    structured = frame.dialogue_structured
    return _clean_subtitle_text(frame.speaker) or _clean_subtitle_text(
        structured.speaker if structured else None
    )


def _format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return _SRT_TIMESTAMP_PATTERN.format(
        hours=hours,
        minutes=minutes,
        seconds=whole_seconds,
        milliseconds=milliseconds,
    )


def _build_srt(frames: list[StoryboardFrame], durations: list[float]) -> str:
    if len(frames) != len(durations):
        raise ValueError("分镜与视频时长数量不一致")

    cues: list[str] = []
    cursor = 0.0
    cue_number = 1
    for frame, raw_duration in zip(frames, durations, strict=True):
        duration = max(0.001, float(raw_duration))
        dialogue = _effective_dialogue(frame)
        if dialogue:
            speaker = _effective_speaker(frame)
            display_text = f"{speaker}：{dialogue}" if speaker else dialogue
            wrapped_text = "\n".join(
                textwrap.wrap(
                    display_text,
                    width=24,
                    break_long_words=True,
                    break_on_hyphens=False,
                )
            )
            cues.append(
                "\n".join(
                    [
                        str(cue_number),
                        f"{_format_srt_timestamp(cursor)} --> "
                        f"{_format_srt_timestamp(cursor + duration)}",
                        wrapped_text,
                    ]
                )
            )
            cue_number += 1
        cursor += duration
    return "\n\n".join(cues) + ("\n" if cues else "")


def _probe_video_duration(ffmpeg_path: str, clip_path: Path) -> float | None:
    ffprobe_path = str(Path(ffmpeg_path).with_name("ffprobe"))
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(clip_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            if duration > 0:
                return duration
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


class StoryboardValidationError(ValueError):
    pass


class CloudStoryboardService:
    FRAME_MEDIA_FIELDS = {
        "video": "video_url",
        "audio": "audio_url",
        "sfx": "sfx_url",
        "dubbed_video": "dubbed_video_url",
        "background_audio": "bg_audio_url",
        "preview_video": "preview_video_url",
    }
    PROJECT_MEDIA_FIELDS = {
        "merged_video": "merged_video_url",
        "background_music": "bgm_url",
    }

    def __init__(
        self,
        database: Database,
        media_storage: CloudMediaStorage | None = None,
    ) -> None:
        self.database = database
        self.projects = PostgresProjectRepository(database)
        self.media = PostgresMediaRepository(database)
        self.media_storage = media_storage
        self.assets = CloudAssetService(database)

    @staticmethod
    def _ensure_version(
        stored: VersionedDocument[Script],
        expected_version: int,
    ) -> None:
        if expected_version < 1:
            raise StoryboardValidationError("内容版本必须为正整数")
        if stored.version != expected_version:
            raise OptimisticVersionConflictError(
                "内容已在其他位置更新，请刷新后重试"
            )

    @staticmethod
    def _require_frame(script: Script, frame_id: str) -> StoryboardFrame:
        frame = next((item for item in script.frames if item.id == frame_id), None)
        if frame is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return frame

    def _dialogue_assets(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> tuple[dict[str, Character], dict[str, CustomVoice]]:
        characters: dict[str, Character] = {}
        voices: dict[str, CustomVoice] = {}
        for asset in self.assets.list_project_assets(context, project_id):
            if isinstance(asset.document, Character):
                characters[asset.domain_id] = asset.document
            elif isinstance(asset.document, CustomVoice):
                voices[asset.domain_id] = asset.document
        return characters, voices

    @staticmethod
    def _dialogue_character(
        frame: StoryboardFrame,
        characters: Mapping[str, Character],
    ) -> Character | None:
        if frame.character_ids:
            character = characters.get(frame.character_ids[0])
            if character is not None:
                return character
        speaker = _effective_speaker(frame)
        if not speaker:
            return None
        normalized = speaker.lower()
        by_name = {item.name.strip().lower(): item for item in characters.values()}
        character = by_name.get(normalized)
        if character is not None:
            return character
        return next(
            (
                item
                for name, item in by_name.items()
                if normalized in name or name in normalized
            ),
            None,
        )

    def build_dialogue_batch_content(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        stored = self.projects.require(context, project_id)
        characters, custom_voices = self._dialogue_assets(context, project_id)
        items: list[dict[str, Any]] = []
        skipped = 0
        no_voice = 0
        dialogue_count = 0
        for frame in stored.document.frames:
            text = _effective_dialogue_text(frame).strip()
            if not text:
                continue
            dialogue_count += 1
            character = self._dialogue_character(frame, characters)
            if character is None or not character.voice_id:
                no_voice += 1
                continue
            if frame.audio_url and not dialogue_audio_is_stale(frame, character):
                skipped += 1
                continue
            instructions = _effective_instructions(frame)
            item: dict[str, Any] = {
                "frame_id": frame.id,
                "text": text,
                "voice_id": character.voice_id,
                "speed": character.voice_speed,
                "pitch": character.voice_pitch,
                "volume": character.voice_volume,
                "instructions": instructions,
                "dialogue_text_hash": _compute_dialogue_hash(
                    text,
                    character.voice_id,
                    instructions,
                ),
            }
            custom_voice = custom_voices.get(character.voice_id)
            if custom_voice is not None:
                item["model_override"] = custom_voice.target_model
                item["family_override"] = custom_voice.family
            items.append(item)
        total_characters = sum(len(str(item["text"])) for item in items)
        if total_characters > 20000:
            raise StoryboardValidationError("单次对白生成不能超过 20000 个字符")
        stats = {
            "generated": 0,
            "skipped": skipped,
            "failed": 0,
            "no_voice": no_voice,
            "total": dialogue_count,
        }
        return {
            "operation": "audio.dialogue.batch",
            "project_version": stored.version,
            "items": items,
            "preflight_stats": stats,
        }, stats

    def apply_dialogue_audio_results(
        self,
        context: WorkspaceContext,
        project_id: str,
        content: Mapping[str, Any],
        media_ids: list[str],
    ) -> dict[str, int]:
        raw_items = content.get("items")
        if not isinstance(raw_items, list) or len(raw_items) != len(media_ids):
            raise StoryboardValidationError("对白任务结果数量与请求不一致")
        preflight = content.get("preflight_stats")
        stats = {
            "generated": 0,
            "skipped": int(preflight.get("skipped", 0)) if isinstance(preflight, Mapping) else 0,
            "failed": 0,
            "no_voice": int(preflight.get("no_voice", 0)) if isinstance(preflight, Mapping) else 0,
            "total": int(preflight.get("total", len(raw_items))) if isinstance(preflight, Mapping) else len(raw_items),
        }
        for media_id in media_ids:
            self._require_project_media(context, project_id, media_id)

        for _attempt in range(3):
            stored = self.projects.require(context, project_id)
            script = stored.document.model_copy(deep=True)
            characters, _custom_voices = self._dialogue_assets(context, project_id)
            generated = 0
            failed = 0
            for raw_item, media_id in zip(raw_items, media_ids, strict=True):
                if not isinstance(raw_item, Mapping):
                    raise StoryboardValidationError("对白任务条目格式无效")
                frame_id = str(raw_item.get("frame_id") or "")
                frame = next((item for item in script.frames if item.id == frame_id), None)
                if frame is None:
                    failed += 1
                    continue
                character = self._dialogue_character(frame, characters)
                text = _effective_dialogue_text(frame).strip()
                instructions = _effective_instructions(frame)
                expected_hash = _compute_dialogue_hash(
                    text,
                    character.voice_id if character else None,
                    instructions,
                )
                if (
                    character is None
                    or character.voice_id != raw_item.get("voice_id")
                    or text != raw_item.get("text")
                    or expected_hash != raw_item.get("dialogue_text_hash")
                ):
                    failed += 1
                    continue
                frame.audio_url = f"media:{media_id}"
                frame.audio_error = None
                frame.status = GenerationStatus.COMPLETED
                frame.dialogue_voice_id = character.voice_id
                frame.dialogue_instructions = instructions
                frame.dialogue_text_hash = expected_hash
                frame.updated_at = time.time()
                generated += 1
            if generated == 0:
                stats["failed"] = failed
                return stats
            script.updated_at = time.time()
            try:
                self.projects.update(
                    context,
                    project_id,
                    Script.model_validate(script.model_dump(mode="json")),
                    stored.version,
                )
            except OptimisticVersionConflictError:
                continue
            stats["generated"] = generated
            stats["failed"] = failed
            return stats
        raise OptimisticVersionConflictError("内容持续更新，无法写入对白音频")

    def _mutate_project(
        self,
        context: WorkspaceContext,
        project_id: str,
        expected_version: int,
        mutate: Callable[[Script], None],
    ) -> VersionedDocument[Script]:
        stored = self.projects.require(context, project_id)
        self._ensure_version(stored, expected_version)
        script = stored.document.model_copy(deep=True)
        mutate(script)
        script.updated_at = time.time()
        return self.projects.update(
            context,
            project_id,
            Script.model_validate(script.model_dump(mode="json")),
            expected_version,
        )

    def _require_project_media(
        self,
        context: WorkspaceContext,
        project_id: str,
        media_id: str,
    ) -> None:
        media = self.media.require(context, media_id)
        if media.project_id is not None and str(media.project_id) != project_id:
            raise ScopedDocumentNotFoundError("资源不存在")

    def validate_frame_update(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        expected_version: int,
    ) -> None:
        stored = self.projects.require(context, project_id)
        self._ensure_version(stored, expected_version)
        self._require_frame(stored.document, frame_id)

    def update_frame(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        updates: Mapping[str, Any],
        expected_version: int,
    ) -> VersionedDocument[Script]:
        allowed = {
            "image_prompt",
            "action_description",
            "dialogue",
            "camera_angle",
            "scene_id",
            "character_ids",
            "prop_ids",
            "duration",
            "shot_size",
            "camera_movement",
            "transition_hint",
        }

        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            payload = frame.model_dump(mode="json")
            payload.update(
                {key: value for key, value in updates.items() if key in allowed}
            )
            replacement = StoryboardFrame.model_validate(payload)
            script.frames[script.frames.index(frame)] = replacement

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def add_frame(
        self,
        context: WorkspaceContext,
        project_id: str,
        *,
        scene_id: str,
        action_description: str,
        camera_angle: str,
        insert_at: int | None,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        frame = StoryboardFrame(
            id=f"frame_{uuid.uuid4().hex[:12]}",
            scene_id=scene_id,
            action_description=action_description,
            camera_angle=camera_angle,
        )

        def mutate(script: Script) -> None:
            if insert_at is None:
                script.frames.append(frame)
                return
            if insert_at < 0 or insert_at > len(script.frames):
                raise StoryboardValidationError("分镜插入位置无效")
            script.frames.insert(insert_at, frame)

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def replace_generated_frames(
        self,
        context: WorkspaceContext,
        project_id: str,
        frames: list[Mapping[str, Any]],
        expected_version: int,
    ) -> VersionedDocument[Script]:
        allowed = {
            "scene_id",
            "character_ids",
            "prop_ids",
            "action_description",
            "facial_expression",
            "dialogue",
            "speaker",
            "visual_atmosphere",
            "character_acting",
            "key_action_physics",
            "shot_size",
            "camera_angle",
            "camera_movement",
            "composition",
            "atmosphere",
            "duration",
            "visual_description",
            "transition_hint",
            "image_prompt",
        }
        replacements: list[StoryboardFrame] = []
        for item in frames:
            payload = {key: value for key, value in item.items() if key in allowed}
            payload["id"] = f"frame_{uuid.uuid4().hex[:12]}"
            payload.setdefault("scene_id", "")
            replacements.append(StoryboardFrame.model_validate(payload))

        if not replacements:
            raise StoryboardValidationError("分镜结果不能为空")

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            lambda script: setattr(script, "frames", replacements),
        )

    def delete_frame(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            script.frames.remove(frame)

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def copy_frame(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        insert_at: int | None,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        def mutate(script: Script) -> None:
            source = self._require_frame(script, frame_id)
            duplicate = copy.deepcopy(source)
            duplicate.id = f"frame_{uuid.uuid4().hex[:12]}"
            duplicate.updated_at = time.time()
            target_index = (
                script.frames.index(source) + 1
                if insert_at is None
                else insert_at
            )
            if target_index < 0 or target_index > len(script.frames):
                raise StoryboardValidationError("分镜插入位置无效")
            script.frames.insert(target_index, duplicate)

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def reorder_frames(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_ids: list[str],
        expected_version: int,
    ) -> VersionedDocument[Script]:
        def mutate(script: Script) -> None:
            existing = {frame.id: frame for frame in script.frames}
            if len(frame_ids) != len(set(frame_ids)) or set(frame_ids) != set(existing):
                raise StoryboardValidationError("分镜排序列表与项目内容不一致")
            script.frames = [existing[frame_id] for frame_id in frame_ids]

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def toggle_frame_lock(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            frame.locked = not frame.locked
            frame.updated_at = time.time()

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def update_workbench(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        updates: Mapping[str, Any],
        expected_version: int,
    ) -> VersionedDocument[Script]:
        allowed = {
            "workbench_tab_mode",
            "t2i_selected_index",
            "workbench_generate_count",
            "t2i_image_urls",
        }
        if "t2i_image_urls" in updates:
            references = updates["t2i_image_urls"]
            if not isinstance(references, list):
                raise StoryboardValidationError("T2I 首帧媒体无效")
            for reference in references:
                if not isinstance(reference, str) or not reference.startswith("media:"):
                    raise StoryboardValidationError("T2I 首帧必须使用云端媒体")
                media_id = reference.removeprefix("media:")
                if not media_id:
                    raise StoryboardValidationError("T2I 首帧媒体无效")
                self._require_project_media(context, project_id, media_id)

        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            payload = frame.model_dump(mode="json")
            payload.update(
                {key: value for key, value in updates.items() if key in allowed}
            )
            replacement = StoryboardFrame.model_validate(payload)
            if replacement.workbench_tab_mode not in {
                None,
                "t2i_i2v",
                "direct_r2v",
            }:
                raise StoryboardValidationError("分镜工作台模式无效")
            if not 1 <= replacement.workbench_generate_count <= 6:
                raise StoryboardValidationError("单次生成数量必须在 1 到 6 之间")
            if replacement.t2i_selected_index < 0:
                raise StoryboardValidationError("T2I 首帧选择无效")
            if replacement.t2i_image_urls:
                if replacement.t2i_selected_index >= len(replacement.t2i_image_urls):
                    raise StoryboardValidationError("T2I 首帧选择无效")
            elif replacement.t2i_selected_index != 0:
                raise StoryboardValidationError("T2I 首帧选择无效")
            script.frames[script.frames.index(frame)] = replacement

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def attach_frame_media(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        media_kind: str,
        media_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        self._require_project_media(context, project_id, media_id)
        reference = f"media:{media_id}"

        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            if media_kind == "rendered_image":
                if frame.rendered_image_asset is None:
                    frame.rendered_image_asset = ImageAsset()
                variant = ImageVariant(
                    id=f"variant_{uuid.uuid4().hex[:12]}",
                    url=reference,
                    is_uploaded_source=True,
                    upload_type="image",
                )
                frame.rendered_image_asset.variants.append(variant)
                frame.rendered_image_asset.selected_id = variant.id
                frame.rendered_image_url = reference
            elif media_kind == "t2i_image":
                frame.t2i_image_urls.append(reference)
                frame.t2i_image_urls = frame.t2i_image_urls[-10:]
                frame.t2i_selected_index = len(frame.t2i_image_urls) - 1
                frame.image_url = reference
            else:
                field_name = self.FRAME_MEDIA_FIELDS.get(media_kind)
                if field_name is None:
                    raise StoryboardValidationError("分镜媒体类型无效")
                setattr(frame, field_name, reference)
            frame.updated_at = time.time()

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def attach_generated_video(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        *,
        task_id: str,
        media_id: str,
        prompt: str,
        image_url: str,
        duration: int,
        resolution: str,
        model: str,
        generation_mode: str,
        workbench_tab: str | None,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        self._require_project_media(context, project_id, media_id)
        reference = f"media:{media_id}"

        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            candidate = next(
                (item for item in script.video_tasks if item.id == task_id),
                None,
            )
            if candidate is None:
                candidate = VideoTask(
                    id=task_id,
                    project_id=project_id,
                    frame_id=frame_id,
                    image_url=image_url,
                    prompt=prompt,
                    status="completed",
                    video_url=reference,
                    duration=duration,
                    resolution=resolution,
                    model=model,
                    generation_mode=generation_mode,
                    workbench_tab=workbench_tab,
                )
                script.video_tasks.append(candidate)
            else:
                candidate.status = "completed"
                candidate.video_url = reference
            if not frame.is_video_pinned:
                frame.selected_video_id = candidate.id
                frame.video_url = reference
            frame.updated_at = time.time()

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def select_frame_video(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        video_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            candidate = next(
                (
                    item
                    for item in script.video_tasks
                    if item.id == video_id
                    and item.frame_id == frame_id
                    and item.status == "completed"
                    and item.video_url
                ),
                None,
            )
            if candidate is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            frame.selected_video_id = candidate.id
            frame.video_url = candidate.video_url
            frame.is_video_pinned = True
            frame.updated_at = time.time()

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def auto_select_latest_video(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            if frame.is_video_pinned:
                return
            candidates = [
                item
                for item in script.video_tasks
                if item.frame_id == frame_id
                and item.status == "completed"
                and item.video_url
            ]
            if not candidates:
                return
            latest = max(candidates, key=lambda item: item.created_at)
            frame.selected_video_id = latest.id
            frame.video_url = latest.video_url
            frame.updated_at = time.time()

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def unpin_frame_video(
        self,
        context: WorkspaceContext,
        project_id: str,
        frame_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        def mutate(script: Script) -> None:
            frame = self._require_frame(script, frame_id)
            frame.is_video_pinned = False
            frame.updated_at = time.time()

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def attach_project_media(
        self,
        context: WorkspaceContext,
        project_id: str,
        media_kind: str,
        media_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        self._require_project_media(context, project_id, media_id)
        field_name = self.PROJECT_MEDIA_FIELDS.get(media_kind)
        if field_name is None:
            raise StoryboardValidationError("项目媒体类型无效")
        return self._mutate_project(
            context,
            project_id,
            expected_version,
            lambda script: setattr(script, field_name, f"media:{media_id}"),
        )

    def merge_videos(
        self,
        context: WorkspaceContext,
        project_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        if self.media_storage is None:
            raise StoryboardValidationError("云端媒体存储尚未启用")
        stored = self.projects.require(context, project_id)
        self._ensure_version(stored, expected_version)
        script = stored.document
        task_by_id = {task.id: task for task in script.video_tasks}
        selected_media_ids: list[str] = []
        selected_tasks: list[VideoTask] = []
        for frame in script.frames:
            task = task_by_id.get(frame.selected_video_id or "")
            if task is None or task.status != "completed" or not task.video_url:
                raise StoryboardValidationError("请先为每个分镜选择已完成的视频")
            if not task.video_url.startswith("media:"):
                raise StoryboardValidationError("分镜视频不是可合成的云端媒体")
            selected_media_ids.append(task.video_url.removeprefix("media:"))
            selected_tasks.append(task)
        if not selected_media_ids:
            raise StoryboardValidationError("项目没有可合成的视频")

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise StoryboardValidationError("服务器未安装 FFmpeg，无法合成视频")

        with tempfile.TemporaryDirectory(prefix="lumenx-merge-") as temp_dir:
            root = Path(temp_dir)
            clip_paths: list[Path] = []
            for index, media_id in enumerate(selected_media_ids):
                content = self.media_storage.read_bytes(context, media_id)
                path = root / f"clip-{index:04d}.mp4"
                path.write_bytes(content)
                clip_paths.append(path)
            durations = [
                _probe_video_duration(ffmpeg, clip_path)
                or float(task.duration or frame.duration or 5.0)
                for clip_path, task, frame in zip(
                    clip_paths,
                    selected_tasks,
                    script.frames,
                    strict=True,
                )
            ]
            subtitle_path = root / "subtitles.srt"
            subtitle_content = _build_srt(script.frames, durations)
            subtitle_filter_args: list[str] = []
            if subtitle_content:
                subtitle_path.write_text(subtitle_content, encoding="utf-8")
                subtitle_filter_args = [
                    "-vf",
                    f"subtitles={subtitle_path.as_posix()}:"
                    f"force_style='{_SUBTITLE_STYLE}'",
                ]
            concat_file = root / "inputs.txt"
            concat_file.write_text(
                "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
                encoding="utf-8",
            )
            output_path = root / "merged.mp4"
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    *subtitle_filter_args,
                    "-c:a",
                    "aac",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                timeout=1200,
                check=False,
            )
            if result.returncode != 0 or not output_path.is_file():
                detail = result.stderr.strip().splitlines()[-1:] or ["未知错误"]
                raise StoryboardValidationError(f"视频合成失败：{detail[0]}")
            content = output_path.read_bytes()

        merged = self.media_storage.store(
            context,
            MediaWrite(
                content=content,
                content_type="video/mp4",
                filename=f"project-{project_id}-merged.mp4",
                project_id=project_id,
                provenance={
                    "origin": "project_merge",
                    "source_media_ids": selected_media_ids,
                },
            ),
        )
        try:
            return self.attach_project_media(
                context,
                project_id,
                "merged_video",
                merged.media_id,
                expected_version,
            )
        except Exception:
            self.media_storage.delete(context, merged.media_id)
            raise

    def update_audio_mix(
        self,
        context: WorkspaceContext,
        project_id: str,
        *,
        background_music_media_id: str | None,
        clear_background_music: bool,
        volume_updates: Mapping[str, int | None],
        expected_version: int,
    ) -> VersionedDocument[Script]:
        if background_music_media_id is not None:
            self._require_project_media(
                context,
                project_id,
                background_music_media_id,
            )

        def mutate(script: Script) -> None:
            if clear_background_music:
                script.bgm_url = None
            elif background_music_media_id is not None:
                script.bgm_url = f"media:{background_music_media_id}"
            mix = dict(
                script.mix_settings
                or {"dialogue": 100, "bgm": 35, "sfx": 60}
            )
            for key, value in volume_updates.items():
                if value is not None:
                    mix[key] = max(0, min(100, value))
            script.mix_settings = mix

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )
