from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from src.apps.comic_gen.llm import ScriptProcessor
from src.apps.comic_gen.models import ArtDirection, PromptConfig, Script, Series

from .ai_request_policy import cloud_execution_payload
from .asset_repositories import PostgresProjectAssetRepository
from .asset_repositories import PostgresSeriesAssetRepository
from .content_repositories import (
    DocumentPayloadValidationError,
    OptimisticVersionConflictError,
    PostgresProjectRepository,
    PostgresSeriesRepository,
)
from .contracts import VersionedDocument, WorkspaceContext
from .database import Database


class NextEpisodeHookTextRequiredError(ValueError):
    pass


class PreviousEpisodeSummaryUnavailableError(ValueError):
    pass


def _text_revision(text: str) -> str:
    digest = hashlib.md5(
        text.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return f"{len(text)}-{digest}"


def _name_match_confidence(local_name: str, series_name: str) -> int:
    local = (local_name or "").strip().lower()
    shared = (series_name or "").strip().lower()
    if not local or not shared:
        return 0
    if local == shared:
        return 100
    if local in shared or shared in local:
        return 75
    return 0


class CloudContentService:
    def __init__(
        self,
        database: Database,
        *,
        script_processor: ScriptProcessor | None = None,
    ) -> None:
        self.database = database
        self.projects = PostgresProjectRepository(database)
        self.series = PostgresSeriesRepository(database)
        self.project_assets = PostgresProjectAssetRepository(database)
        self.series_assets = PostgresSeriesAssetRepository(database)
        self.script_processor = script_processor or ScriptProcessor()

    @staticmethod
    def _ensure_expected_version(
        stored: VersionedDocument[Any],
        expected_version: int,
    ) -> None:
        if expected_version < 1:
            raise ValueError("内容版本必须为正整数")
        if stored.version != expected_version:
            raise OptimisticVersionConflictError(
                "内容已在其他位置更新，请刷新后重试"
            )

    @staticmethod
    def _validated_script(script: Script) -> Script:
        return Script.model_validate(script.model_dump(mode="json"))

    @staticmethod
    def _validated_series(series: Series) -> Series:
        return Series.model_validate(series.model_dump(mode="json"))

    @staticmethod
    def _extract_project_assets(script: Script) -> list[tuple[str, Any]]:
        assets = [
            *(("character", item.model_copy(deep=True)) for item in script.characters),
            *(("scene", item.model_copy(deep=True)) for item in script.scenes),
            *(("prop", item.model_copy(deep=True)) for item in script.props),
        ]
        script.characters = []
        script.scenes = []
        script.props = []
        return assets

    def _add_project_assets(
        self,
        context: WorkspaceContext,
        project_id: str,
        assets: list[tuple[str, Any]],
        *,
        session: Any,
    ) -> None:
        for asset_type, document in assets:
            self.project_assets.add(
                context,
                project_id,
                asset_type,
                document,
                provenance={"origin": "script_analysis"},
                session=session,
            )

    def list_projects(
        self,
        context: WorkspaceContext,
    ) -> list[VersionedDocument[Script]]:
        return self.projects.list(context)

    def get_project(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> VersionedDocument[Script]:
        return self.projects.require(context, project_id)

    def get_next_episode_hook(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> dict[str, Any]:
        stored = self.projects.require(context, project_id)
        script = stored.document
        text = script.original_text or ""
        return {
            "has_text": bool(text.strip()),
            "hook": script.next_hook_cache,
            "stale": (
                script.next_hook_cache is not None
                and script.next_hook_revision != _text_revision(text)
            ),
            "version": stored.version,
        }

    def build_next_episode_hook_prompt(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> tuple[str, int]:
        stored = self.projects.require(context, project_id)
        text = stored.document.original_text or ""
        if not text.strip():
            raise NextEpisodeHookTextRequiredError("请先填写本集剧本文本")
        prompt = (
            "下面是一集剧本的结尾。请你站在编剧的视角，预测下一集开头可能的 hook，"
            "用 120-180 字给出 2-3 个具体方向（包括场景、角色出场、悬念点）。"
            "不要列点编号，写成连贯的中文段落，便于作者参考。\n\n"
            f"本集结尾：\n{text[-1500:]}"
        )
        return prompt, stored.version

    def update_next_episode_hook(
        self,
        context: WorkspaceContext,
        project_id: str,
        hook: str | None,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        normalized_hook = hook.strip() if hook is not None else None
        if not normalized_hook:
            normalized_hook = None

        def mutate(script: Script) -> None:
            script.next_hook_cache = normalized_hook
            script.next_hook_revision = (
                _text_revision(script.original_text or "")
                if normalized_hook is not None
                else None
            )

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def _previous_episode(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> tuple[
        VersionedDocument[Script],
        str | None,
        VersionedDocument[Script] | None,
    ]:
        current = self.projects.require(context, project_id)
        series_id = current.document.series_id
        if not series_id:
            return current, None, None
        series = self.series.get(context, series_id)
        if series is None:
            return current, None, None
        try:
            episode_index = series.document.episode_ids.index(project_id)
        except ValueError:
            return current, None, None
        if episode_index <= 0:
            return current, None, None
        previous_id = series.document.episode_ids[episode_index - 1]
        return current, previous_id, self.projects.get(context, previous_id)

    @staticmethod
    def _previous_episode_frames(script: Script) -> list[dict[str, Any]]:
        last_frames: list[dict[str, Any]] = []
        for frame in (script.frames or [])[-4:]:
            thumbnail_url = frame.video_url or frame.rendered_image_url or frame.image_url
            if not thumbnail_url and frame.t2i_image_urls:
                selected_index = max(
                    0,
                    min(frame.t2i_selected_index or 0, len(frame.t2i_image_urls) - 1),
                )
                thumbnail_url = frame.t2i_image_urls[selected_index]
            last_frames.append(
                {
                    "id": frame.id,
                    "action_description": (frame.action_description or "")[:120],
                    "thumbnail_url": thumbnail_url,
                    "video_url": frame.video_url,
                }
            )
        return last_frames

    def get_previous_episode_summary(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> dict[str, Any]:
        current, previous_id, previous = self._previous_episode(context, project_id)
        base = {
            "previous_episode_id": previous_id,
            "previous_episode_title": (
                previous.document.title if previous is not None else None
            ),
            "raw_snippet": "",
            "ai_summary": None,
            "ai_summary_stale": False,
            "last_frames": [],
            "version": current.version,
        }
        if previous is None or not (previous.document.original_text or "").strip():
            return {"has_previous": False, **base}
        previous_text = previous.document.original_text or ""
        return {
            "has_previous": True,
            **base,
            "raw_snippet": previous_text[-800:],
            "ai_summary": current.document.last_episode_summary_cache,
            "ai_summary_stale": (
                current.document.last_episode_summary_cache is not None
                and current.document.last_episode_summary_revision
                != _text_revision(previous_text)
            ),
            "last_frames": self._previous_episode_frames(previous.document),
        }

    def build_previous_episode_summary_prompt(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> tuple[str, int, str, str, int]:
        current, previous_id, previous = self._previous_episode(context, project_id)
        if previous_id is None:
            raise PreviousEpisodeSummaryUnavailableError("当前项目没有可回顾的上一集")
        if previous is None or not (previous.document.original_text or "").strip():
            raise PreviousEpisodeSummaryUnavailableError("上一集还没有剧本文本")
        prompt = (
            "请用 150-220 字概括下面这段剧本的核心情节、关键角色出场与结尾留下的悬念/钩子。"
            "不要列点，写成一段连贯的中文叙述，便于作者快速回顾上一集走到了哪里。\n\n"
            f"剧本：\n{previous.document.original_text}"
        )
        return (
            prompt,
            current.version,
            previous_id,
            previous.document.title,
            previous.version,
        )

    def update_previous_episode_summary(
        self,
        context: WorkspaceContext,
        project_id: str,
        summary: str | None,
        expected_version: int,
        source_previous_version: int | None = None,
    ) -> VersionedDocument[Script]:
        _current, _previous_id, previous = self._previous_episode(context, project_id)
        if source_previous_version is not None and (
            previous is None or previous.version != source_previous_version
        ):
            raise OptimisticVersionConflictError(
                "上一集内容已更新，请重新生成摘要"
            )
        normalized_summary = summary.strip() if summary is not None else None
        if not normalized_summary:
            normalized_summary = None

        def mutate(script: Script) -> None:
            script.last_episode_summary_cache = normalized_summary
            script.last_episode_summary_revision = (
                _text_revision(previous.document.original_text or "")
                if normalized_summary is not None and previous is not None
                else None
            )

        return self._mutate_project(
            context,
            project_id,
            expected_version,
            mutate,
        )

    def get_project_execution_payload(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> dict[str, Any]:
        return cloud_execution_payload(
            self.projects.require(context, project_id).document
        )

    def get_series_execution_payload(
        self,
        context: WorkspaceContext,
        series_id: str,
    ) -> dict[str, Any]:
        return cloud_execution_payload(
            self.series.require(context, series_id).document
        )

    def create_project(
        self,
        context: WorkspaceContext,
        *,
        title: str,
        text: str,
        skip_analysis: bool,
        workflow_mode: str,
        series_id: str | None = None,
    ) -> VersionedDocument[Script]:
        if skip_analysis:
            script = self.script_processor.create_draft_script(title, text)
        else:
            script = self.script_processor.parse_novel(title, text)
        script.workflow_mode = workflow_mode
        parsed_assets = self._extract_project_assets(script)

        if series_id is None:
            with self.database.transaction(context.identity) as session:
                stored_script = self.projects.add(
                    context,
                    self._validated_script(script),
                    session=session,
                )
                self._add_project_assets(
                    context,
                    stored_script.document.id,
                    parsed_assets,
                    session=session,
                )
                return stored_script

        with self.database.transaction(context.identity) as session:
            stored_series = self.series.require(context, series_id, session=session)
            series = stored_series.document.model_copy(deep=True)
            script.series_id = series.id
            existing_episodes = [
                project.document.episode_number
                for project in self.projects.list(context, session=session)
                if project.document.series_id == series.id
                and project.document.episode_number is not None
            ]
            script.episode_number = max(existing_episodes or [0]) + 1
            stored_script = self.projects.add(
                context,
                self._validated_script(script),
                session=session,
            )
            self._add_project_assets(
                context,
                stored_script.document.id,
                parsed_assets,
                session=session,
            )
            project_id = stored_script.document.id
            if project_id not in series.episode_ids:
                series.episode_ids.append(project_id)
            series.updated_at = time.time()
            self.series.update(
                context,
                series.id,
                self._validated_series(series),
                stored_series.version,
                session=session,
            )
            return stored_script

    def _mutate_project(
        self,
        context: WorkspaceContext,
        project_id: str,
        expected_version: int,
        mutate: Callable[[Script], None],
    ) -> VersionedDocument[Script]:
        stored = self.projects.require(context, project_id)
        self._ensure_expected_version(stored, expected_version)
        script = stored.document.model_copy(deep=True)
        mutate(script)
        script.updated_at = time.time()
        return self.projects.update(
            context,
            project_id,
            self._validated_script(script),
            expected_version,
        )

    def update_project_text(
        self,
        context: WorkspaceContext,
        project_id: str,
        text: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        return self._mutate_project(
            context,
            project_id,
            expected_version,
            lambda script: setattr(script, "original_text", text or ""),
        )

    def toggle_project_starred(
        self,
        context: WorkspaceContext,
        project_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        return self._mutate_project(
            context,
            project_id,
            expected_version,
            lambda script: setattr(script, "starred", not script.starred),
        )

    def reparse_project(
        self,
        context: WorkspaceContext,
        project_id: str,
        text: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        stored = self.projects.require(context, project_id)
        self._ensure_expected_version(stored, expected_version)
        existing = stored.document
        parsed = self.script_processor.parse_novel(
            existing.title,
            text,
            existing.prompt_config.entity_extraction,
        )
        parsed.id = existing.id
        parsed.created_at = existing.created_at
        parsed.updated_at = time.time()
        for field_name in (
            "art_direction",
            "model_settings",
            "style_preset",
            "style_prompt",
            "merged_video_url",
            "workflow_mode",
            "series_id",
            "episode_number",
            "prompt_config",
            "default_generation_mode",
            "bgm_url",
            "mix_settings",
            "starred",
            "last_episode_summary_cache",
            "last_episode_summary_revision",
            "next_hook_cache",
            "next_hook_revision",
        ):
            setattr(parsed, field_name, getattr(existing, field_name))
        parsed_assets = self._extract_project_assets(parsed)
        with self.database.transaction(context.identity) as session:
            updated = self.projects.update(
                context,
                project_id,
                self._validated_script(parsed),
                expected_version,
                session=session,
            )
            for stored_asset in self.project_assets.list(
                context,
                project_id,
                session=session,
            ):
                self.project_assets.soft_delete(
                    context,
                    project_id,
                    stored_asset.record_id,
                    stored_asset.version,
                    session=session,
                )
            self._add_project_assets(
                context,
                project_id,
                parsed_assets,
                session=session,
            )
            return updated

    def apply_entity_extraction(
        self,
        context: WorkspaceContext,
        project_id: str,
        text: str,
        extraction: Mapping[str, Any],
        expected_version: int,
    ) -> VersionedDocument[Script]:
        stored = self.projects.require(context, project_id)
        self._ensure_expected_version(stored, expected_version)
        existing = stored.document
        parsed = self.script_processor.create_script_from_extraction(
            existing.title,
            text,
            dict(extraction),
        )
        parsed.id = existing.id
        parsed.created_at = existing.created_at
        parsed.updated_at = time.time()
        for field_name in (
            "art_direction",
            "model_settings",
            "style_preset",
            "style_prompt",
            "merged_video_url",
            "workflow_mode",
            "series_id",
            "episode_number",
            "prompt_config",
            "default_generation_mode",
            "bgm_url",
            "mix_settings",
            "starred",
            "last_episode_summary_cache",
            "last_episode_summary_revision",
            "next_hook_cache",
            "next_hook_revision",
        ):
            setattr(parsed, field_name, getattr(existing, field_name))
        parsed_assets = self._extract_project_assets(parsed)
        with self.database.transaction(context.identity) as session:
            updated = self.projects.update(
                context,
                project_id,
                self._validated_script(parsed),
                expected_version,
                session=session,
            )
            for stored_asset in self.project_assets.list(
                context,
                project_id,
                session=session,
            ):
                self.project_assets.soft_delete(
                    context,
                    project_id,
                    stored_asset.record_id,
                    stored_asset.version,
                    session=session,
                )
            self._add_project_assets(
                context,
                project_id,
                parsed_assets,
                session=session,
            )
            return updated

    def get_reconcile_suggestions(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        empty = {"characters": [], "scenes": [], "props": []}
        with self.database.transaction(context.identity) as session:
            stored_project = self.projects.require(
                context,
                project_id,
                session=session,
            )
            series_id = stored_project.document.series_id
            if not series_id:
                return empty
            self.series.require(context, series_id, session=session)
            local_assets = self.project_assets.list(
                context,
                project_id,
                session=session,
            )
            shared_assets = self.series_assets.list(
                context,
                series_id,
                session=session,
            )

        plural_names = {
            "character": "characters",
            "scene": "scenes",
            "prop": "props",
        }
        result: dict[str, list[dict[str, Any]]] = {
            "characters": [],
            "scenes": [],
            "props": [],
        }
        shared_by_type = {
            asset_type: [
                asset for asset in shared_assets if asset.asset_type == asset_type
            ]
            for asset_type in plural_names
        }
        for local in local_assets:
            result_key = plural_names.get(local.asset_type)
            if result_key is None:
                continue
            best = None
            best_confidence = 0
            for shared in shared_by_type[local.asset_type]:
                confidence = _name_match_confidence(local.name, shared.name)
                if confidence > best_confidence:
                    best = shared
                    best_confidence = confidence
            result[result_key].append(
                {
                    "local_id": local.domain_id,
                    "local_name": local.name,
                    "suggested_series_id": (
                        best.domain_id if best is not None and best_confidence > 0 else None
                    ),
                    "suggested_series_name": (
                        best.name if best is not None and best_confidence > 0 else None
                    ),
                    "confidence": best_confidence,
                }
            )
        return result

    def apply_reconcile(
        self,
        context: WorkspaceContext,
        project_id: str,
        decisions: Mapping[str, list[Mapping[str, Any]]],
        expected_version: int,
    ) -> VersionedDocument[Script]:
        asset_types = {
            "characters": "character",
            "scenes": "scene",
            "props": "prop",
        }
        replacements: dict[str, dict[str, str]] = {
            asset_type: {} for asset_type in asset_types.values()
        }
        with self.database.transaction(context.identity) as session:
            stored_project = self.projects.require(
                context,
                project_id,
                session=session,
            )
            self._ensure_expected_version(stored_project, expected_version)
            script = stored_project.document.model_copy(deep=True)
            if not script.series_id:
                raise DocumentPayloadValidationError("项目未关联系列")
            self.series.require(context, script.series_id, session=session)

            local_assets = {
                (asset.asset_type, asset.domain_id): asset
                for asset in self.project_assets.list(
                    context,
                    project_id,
                    session=session,
                )
            }
            shared_assets = {
                (asset.asset_type, asset.domain_id): asset
                for asset in self.series_assets.list(
                    context,
                    script.series_id,
                    session=session,
                )
            }
            handled: set[tuple[str, str]] = set()

            for field_name, asset_type in asset_types.items():
                for decision in decisions.get(field_name, []):
                    local_id = str(decision.get("local_id") or "").strip()
                    action = str(decision.get("action") or "").strip()
                    key = (asset_type, local_id)
                    if not local_id or key in handled:
                        raise DocumentPayloadValidationError("实体对齐决策无效或重复")
                    handled.add(key)
                    local = local_assets.get(key)
                    if local is None:
                        raise DocumentPayloadValidationError("待对齐实体不存在或已更新")
                    if action == "skip":
                        continue
                    if action == "merge_into_series":
                        target_id = str(
                            decision.get("target_series_id") or ""
                        ).strip()
                        target = shared_assets.get((asset_type, target_id))
                        if target is None:
                            raise DocumentPayloadValidationError("系列目标实体不存在")
                        replacements[asset_type][local.domain_id] = target.domain_id
                    elif action == "create_new_in_series":
                        if (asset_type, local.domain_id) in shared_assets:
                            raise DocumentPayloadValidationError("系列中已存在该实体")
                        created = self.series_assets.add(
                            context,
                            script.series_id,
                            asset_type,
                            local.document.model_copy(deep=True),
                            provenance={
                                "origin": "reconciled",
                                "source_scope": "project",
                                "source_asset_record_id": local.record_id,
                                "source_domain_id": local.domain_id,
                            },
                            media_object_id=local.media_object_id,
                            session=session,
                        )
                        shared_assets[(asset_type, created.domain_id)] = created
                    else:
                        raise DocumentPayloadValidationError("实体对齐操作无效")

                    self.project_assets.soft_delete(
                        context,
                        project_id,
                        local.record_id,
                        local.version,
                        session=session,
                    )

            for frame in script.frames:
                if frame.scene_id in replacements["scene"]:
                    frame.scene_id = replacements["scene"][frame.scene_id]
                frame.character_ids = [
                    replacements["character"].get(character_id, character_id)
                    for character_id in frame.character_ids
                ]
                frame.prop_ids = [
                    replacements["prop"].get(prop_id, prop_id)
                    for prop_id in frame.prop_ids
                ]
            script.updated_at = time.time()
            return self.projects.update(
                context,
                project_id,
                self._validated_script(script),
                expected_version,
                session=session,
            )

    def delete_project(
        self,
        context: WorkspaceContext,
        project_id: str,
        expected_version: int,
    ) -> Script:
        with self.database.transaction(context.identity) as session:
            stored_project = self.projects.require(context, project_id, session=session)
            self._ensure_expected_version(stored_project, expected_version)
            script = stored_project.document
            if script.series_id:
                stored_series = self.series.get(
                    context,
                    script.series_id,
                    session=session,
                )
                if stored_series and project_id in stored_series.document.episode_ids:
                    series = stored_series.document.model_copy(deep=True)
                    series.episode_ids.remove(project_id)
                    series.updated_at = time.time()
                    self.series.update(
                        context,
                        series.id,
                        self._validated_series(series),
                        stored_series.version,
                        session=session,
                    )
            self.projects.soft_delete(
                context,
                project_id,
                expected_version=expected_version,
                session=session,
            )
            return script

    def get_project_prompt_config(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> tuple[PromptConfig, int]:
        stored = self.projects.require(context, project_id)
        return stored.document.prompt_config, stored.version

    def update_project_prompt_config(
        self,
        context: WorkspaceContext,
        project_id: str,
        prompt_config: PromptConfig,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        return self._mutate_project(
            context,
            project_id,
            expected_version,
            lambda script: setattr(script, "prompt_config", prompt_config),
        )

    def analyze_project_art_direction(
        self,
        context: WorkspaceContext,
        project_id: str,
        script_text: str,
    ) -> list[dict[str, Any]]:
        stored = self.projects.require(context, project_id)
        custom_prompt = stored.document.prompt_config.style_analysis
        return self.script_processor.analyze_script_for_styles(script_text, custom_prompt)

    def save_project_art_direction(
        self,
        context: WorkspaceContext,
        project_id: str,
        art_direction: ArtDirection,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        return self._mutate_project(
            context,
            project_id,
            expected_version,
            lambda script: setattr(script, "art_direction", art_direction),
        )

    def clear_project_art_direction(
        self,
        context: WorkspaceContext,
        project_id: str,
        expected_version: int,
    ) -> VersionedDocument[Script]:
        return self._mutate_project(
            context,
            project_id,
            expected_version,
            lambda script: setattr(script, "art_direction", None),
        )

    def list_series(
        self,
        context: WorkspaceContext,
    ) -> list[VersionedDocument[Series]]:
        return self.series.list(context)

    def get_series(
        self,
        context: WorkspaceContext,
        series_id: str,
    ) -> VersionedDocument[Series]:
        return self.series.require(context, series_id)

    def create_series(
        self,
        context: WorkspaceContext,
        *,
        title: str,
        description: str,
        workflow_mode: str,
        content_mode: str,
        default_generation_mode: str,
    ) -> VersionedDocument[Series]:
        now = time.time()
        series = Series(
            id=str(uuid.uuid4()),
            title=title,
            description=description,
            workflow_mode=workflow_mode,
            content_mode=content_mode,
            default_generation_mode=default_generation_mode,
            created_at=now,
            updated_at=now,
        )
        return self.series.add(context, series)

    def update_series(
        self,
        context: WorkspaceContext,
        series_id: str,
        updates: Mapping[str, Any],
        expected_version: int,
    ) -> VersionedDocument[Series]:
        stored = self.series.require(context, series_id)
        self._ensure_expected_version(stored, expected_version)
        payload = stored.document.model_dump(mode="json")
        payload.update(updates)
        payload["id"] = stored.document.id
        payload["episode_ids"] = stored.document.episode_ids
        payload["created_at"] = stored.document.created_at
        payload["updated_at"] = time.time()
        series = Series.model_validate(payload)
        return self.series.update(
            context,
            series_id,
            series,
            expected_version,
        )

    def delete_series(
        self,
        context: WorkspaceContext,
        series_id: str,
        expected_version: int,
    ) -> Series:
        with self.database.transaction(context.identity) as session:
            stored_series = self.series.require(context, series_id, session=session)
            self._ensure_expected_version(stored_series, expected_version)
            for project_id in stored_series.document.episode_ids:
                stored_project = self.projects.get(
                    context,
                    project_id,
                    session=session,
                )
                if not stored_project or stored_project.document.series_id != series_id:
                    continue
                project = stored_project.document.model_copy(deep=True)
                project.series_id = None
                project.episode_number = None
                project.updated_at = time.time()
                self.projects.update(
                    context,
                    project.id,
                    self._validated_script(project),
                    stored_project.version,
                    session=session,
                )
            self.series.soft_delete(
                context,
                series_id,
                expected_version=expected_version,
                session=session,
            )
            return stored_series.document

    def list_series_episodes(
        self,
        context: WorkspaceContext,
        series_id: str,
    ) -> tuple[VersionedDocument[Series], list[VersionedDocument[Script]]]:
        with self.database.transaction(context.identity) as session:
            stored_series = self.series.require(context, series_id, session=session)
            projects_by_id = {
                project.document.id: project
                for project in self.projects.list(context, session=session)
                if project.document.series_id == series_id
            }
            episodes = [
                projects_by_id[project_id]
                for project_id in stored_series.document.episode_ids
                if project_id in projects_by_id
            ]
            return stored_series, episodes

    def add_episode(
        self,
        context: WorkspaceContext,
        series_id: str,
        project_id: str,
        episode_number: int | None,
        expected_series_version: int,
    ) -> VersionedDocument[Series]:
        with self.database.transaction(context.identity) as session:
            stored_target = self.series.require(context, series_id, session=session)
            self._ensure_expected_version(stored_target, expected_series_version)
            stored_project = self.projects.require(context, project_id, session=session)
            project = stored_project.document.model_copy(deep=True)

            if project.series_id and project.series_id != series_id:
                stored_previous = self.series.require(
                    context,
                    project.series_id,
                    session=session,
                )
                previous = stored_previous.document.model_copy(deep=True)
                if project_id in previous.episode_ids:
                    previous.episode_ids.remove(project_id)
                    previous.updated_at = time.time()
                    self.series.update(
                        context,
                        previous.id,
                        self._validated_series(previous),
                        stored_previous.version,
                        session=session,
                    )

            target = stored_target.document.model_copy(deep=True)
            target_changed = project_id not in target.episode_ids
            if target_changed:
                target.episode_ids.append(project_id)
                target.updated_at = time.time()
            project.series_id = series_id
            project.episode_number = episode_number or target.episode_ids.index(project_id) + 1
            project.updated_at = time.time()
            self.projects.update(
                context,
                project_id,
                self._validated_script(project),
                stored_project.version,
                session=session,
            )
            if not target_changed:
                return stored_target
            return self.series.update(
                context,
                series_id,
                self._validated_series(target),
                expected_series_version,
                session=session,
            )

    def remove_episode(
        self,
        context: WorkspaceContext,
        series_id: str,
        project_id: str,
        expected_series_version: int,
    ) -> VersionedDocument[Series]:
        with self.database.transaction(context.identity) as session:
            stored_series = self.series.require(context, series_id, session=session)
            self._ensure_expected_version(stored_series, expected_series_version)
            stored_project = self.projects.require(context, project_id, session=session)
            if stored_project.document.series_id != series_id:
                raise OptimisticVersionConflictError("项目的系列关联已发生变化")

            series = stored_series.document.model_copy(deep=True)
            if project_id in series.episode_ids:
                series.episode_ids.remove(project_id)
            series.updated_at = time.time()
            project = stored_project.document.model_copy(deep=True)
            project.series_id = None
            project.episode_number = None
            project.updated_at = time.time()
            self.projects.update(
                context,
                project_id,
                self._validated_script(project),
                stored_project.version,
                session=session,
            )
            return self.series.update(
                context,
                series_id,
                self._validated_series(series),
                expected_series_version,
                session=session,
            )

    def get_series_prompt_config(
        self,
        context: WorkspaceContext,
        series_id: str,
    ) -> tuple[PromptConfig, int]:
        stored = self.series.require(context, series_id)
        return stored.document.prompt_config, stored.version

    def update_series_prompt_config(
        self,
        context: WorkspaceContext,
        series_id: str,
        prompt_config: PromptConfig,
        expected_version: int,
    ) -> VersionedDocument[Series]:
        return self.update_series(
            context,
            series_id,
            {"prompt_config": prompt_config.model_dump(mode="json")},
            expected_version,
        )
