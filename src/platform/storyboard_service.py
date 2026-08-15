from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from src.apps.comic_gen.models import ImageAsset, ImageVariant, Script, StoryboardFrame

from .content_repositories import (
    OptimisticVersionConflictError,
    PostgresProjectRepository,
    ScopedDocumentNotFoundError,
)
from .contracts import VersionedDocument, WorkspaceContext
from .database import Database
from .media_storage import PostgresMediaRepository


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

    def __init__(self, database: Database) -> None:
        self.database = database
        self.projects = PostgresProjectRepository(database)
        self.media = PostgresMediaRepository(database)

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
        }

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
