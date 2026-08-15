from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from src.apps.comic_gen.models import (
    AssetUnit,
    Character,
    CustomVoice,
    ImageAsset,
    ImageVariant,
    Prop,
    Scene,
)

from .asset_repositories import (
    AssetResolver,
    AssetScope,
    PostgresProjectAssetRepository,
    PostgresSeriesAssetRepository,
    PostgresWorkspaceAssetRepository,
    ReadOnlySystemAssetRepository,
    StoredAsset,
)
from .content_repositories import (
    DocumentPayloadValidationError,
    OptimisticVersionConflictError,
    PostgresProjectRepository,
    ScopedDocumentNotFoundError,
)
from .contracts import WorkspaceContext
from .database import Database
from .media_storage import PostgresMediaRepository


class AssetMutationForbiddenError(PermissionError):
    pass


def _new_domain_id(asset_type: str) -> str:
    prefix = {
        "character": "char",
        "scene": "scene",
        "prop": "prop",
        "voice": "voice",
    }.get(asset_type, "asset")
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _document_with_new_id(document: BaseModel | dict[str, Any]) -> BaseModel | dict[str, Any]:
    if isinstance(document, BaseModel):
        payload = document.model_dump(mode="json")
        payload["id"] = _new_domain_id(
            "voice" if isinstance(document, CustomVoice) else _asset_type(document)
        )
        return type(document).model_validate(payload)
    payload = copy.deepcopy(document)
    payload["id"] = _new_domain_id("other")
    return payload


def _asset_type(document: BaseModel | dict[str, Any]) -> str:
    if isinstance(document, Character):
        return "character"
    if isinstance(document, Scene):
        return "scene"
    if isinstance(document, Prop):
        return "prop"
    if isinstance(document, CustomVoice):
        return "voice"
    return "other"


class CloudAssetService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.projects = PostgresProjectRepository(database)
        self.project_assets = PostgresProjectAssetRepository(database)
        self.series_assets = PostgresSeriesAssetRepository(database)
        self.workspace_assets = PostgresWorkspaceAssetRepository(database)
        self.system_assets = ReadOnlySystemAssetRepository(database)
        self.resolver = AssetResolver(database)
        self.media = PostgresMediaRepository(database)

    @staticmethod
    def _ensure_version(asset: StoredAsset, expected_version: int) -> None:
        if asset.version != expected_version:
            raise OptimisticVersionConflictError(
                "资产已在其他位置更新，请刷新后重试"
            )

    @staticmethod
    def _create_document(
        asset_type: str,
        *,
        name: str,
        description: str,
        persona: str = "",
        image_url: str | None = None,
        voice_id: str | None = None,
        **attributes: Any,
    ) -> BaseModel:
        domain_id = _new_domain_id(asset_type)
        if asset_type == "character":
            reference_sheet = AssetUnit()
            if image_url:
                variant = ImageVariant(id=_new_domain_id("other"), url=image_url)
                reference_sheet.image_variants.append(variant)
                reference_sheet.selected_image_id = variant.id
            document: BaseModel = Character(
                id=domain_id,
                name=name,
                description=description,
                persona=persona,
                image_url=image_url,
                voice_id=voice_id,
                reference_sheet=reference_sheet,
            )
        elif asset_type == "scene":
            image_asset = ImageAsset()
            if image_url:
                variant = ImageVariant(id=_new_domain_id("other"), url=image_url)
                image_asset.variants.append(variant)
                image_asset.selected_id = variant.id
            document = Scene(
                id=domain_id,
                name=name,
                description=description,
                image_url=image_url,
                image_asset=image_asset,
            )
        elif asset_type == "prop":
            image_asset = ImageAsset()
            if image_url:
                variant = ImageVariant(id=_new_domain_id("other"), url=image_url)
                image_asset.variants.append(variant)
                image_asset.selected_id = variant.id
            document = Prop(
                id=domain_id,
                name=name,
                description=description,
                image_url=image_url,
                image_asset=image_asset,
            )
        else:
            raise DocumentPayloadValidationError("仅支持角色、场景和道具资产")
        if attributes:
            CloudAssetService._patch_document(document, attributes)
        return document

    def list_project_assets(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> list[StoredAsset]:
        effective: dict[tuple[str, str], StoredAsset] = {}
        with self.database.transaction(context.identity) as session:
            project = self.projects.require(context, project_id, session=session)
            scopes = [
                self.project_assets.list(
                    context,
                    project_id,
                    session=session,
                )
            ]
            if project.document.series_id is not None:
                scopes.append(
                    self.series_assets.list(
                        context,
                        project.document.series_id,
                        session=session,
                    )
                )
            scopes.extend(
                [
                    self.workspace_assets.list(context, session=session),
                    self.system_assets.list(context, session=session),
                ]
            )
            for assets in scopes:
                for asset in assets:
                    effective.setdefault(
                        (asset.asset_type, asset.domain_id),
                        asset,
                    )
        return list(effective.values())

    def create_project_asset(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        media_id: str | None = None,
        **payload: Any,
    ) -> StoredAsset:
        if media_id is not None:
            media = self.media.require(context, media_id)
            if media.project_id is not None and str(media.project_id) != project_id:
                raise ScopedDocumentNotFoundError("资源不存在")
            payload["image_url"] = f"media:{media_id}"
        document = self._create_document(asset_type, **payload)
        return self.project_assets.add(
            context,
            project_id,
            asset_type,
            document,
            media_object_id=media_id,
        )

    def create_series_asset(
        self,
        context: WorkspaceContext,
        series_id: str,
        asset_type: str,
        media_id: str | None = None,
        **payload: Any,
    ) -> StoredAsset:
        if media_id is not None:
            media = self.media.require(context, media_id)
            if media.project_id is not None:
                raise ScopedDocumentNotFoundError("资源不存在")
            payload["image_url"] = f"media:{media_id}"
        document = self._create_document(asset_type, **payload)
        return self.series_assets.add(
            context,
            series_id,
            asset_type,
            document,
            media_object_id=media_id,
        )

    def create_library_asset(
        self,
        context: WorkspaceContext,
        asset_type: str,
        media_id: str | None = None,
        **payload: Any,
    ) -> StoredAsset:
        if media_id is not None:
            media = self.media.require(context, media_id)
            if media.project_id is not None:
                raise ScopedDocumentNotFoundError("资源不存在")
            payload["image_url"] = f"media:{media_id}"
        document = self._create_document(asset_type, **payload)
        return self.workspace_assets.add(
            context,
            None,
            asset_type,
            document,
            media_object_id=media_id,
        )

    def list_series_assets(
        self,
        context: WorkspaceContext,
        series_id: str,
    ) -> list[StoredAsset]:
        return self.series_assets.list(context, series_id)

    def list_library_assets(self, context: WorkspaceContext) -> list[StoredAsset]:
        effective: dict[tuple[str, str], StoredAsset] = {}
        for asset in self.workspace_assets.list(context):
            effective[(asset.asset_type, asset.domain_id)] = asset
        for asset in self.system_assets.list(context):
            effective.setdefault((asset.asset_type, asset.domain_id), asset)
        return list(effective.values())

    def _project_series_id(
        self,
        context: WorkspaceContext,
        project_id: str,
    ) -> str | None:
        return self.projects.require(context, project_id).document.series_id

    def _project_mutation_target(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
    ) -> tuple[StoredAsset, Any, str | None]:
        stored = self.resolver.resolve(
            context,
            asset_type,
            domain_id,
            project_id=project_id,
        )
        if stored is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        if stored.scope is AssetScope.PROJECT:
            return stored, self.project_assets, project_id
        if stored.scope is AssetScope.SERIES:
            series_id = self._project_series_id(context, project_id)
            if series_id is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            return stored, self.series_assets, series_id
        raise AssetMutationForbiddenError(
            "工作区或系统资产需要先复制到项目后才能修改"
        )

    def _mutate_project_asset(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        expected_version: int,
        mutate: Callable[[BaseModel | dict[str, Any]], None],
        *,
        media_object_id: str | None = None,
    ) -> StoredAsset:
        stored, repository, parent_id = self._project_mutation_target(
            context,
            project_id,
            asset_type,
            domain_id,
        )
        self._ensure_version(stored, expected_version)
        document = copy.deepcopy(stored.document)
        mutate(document)
        return repository.update(
            context,
            parent_id,
            stored.record_id,
            document,
            expected_version,
            media_object_id=media_object_id,
        )

    def _mutate_series_asset(
        self,
        context: WorkspaceContext,
        series_id: str,
        asset_type: str,
        domain_id: str,
        expected_version: int,
        mutate: Callable[[BaseModel | dict[str, Any]], None],
        *,
        media_object_id: str | None = None,
    ) -> StoredAsset:
        stored = self.series_assets.find_by_domain_id(
            context,
            series_id,
            asset_type,
            domain_id,
        )
        if stored is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        self._ensure_version(stored, expected_version)
        document = copy.deepcopy(stored.document)
        mutate(document)
        return self.series_assets.update(
            context,
            series_id,
            stored.record_id,
            document,
            expected_version,
            media_object_id=media_object_id,
        )

    def _mutate_library_asset(
        self,
        context: WorkspaceContext,
        asset_type: str,
        domain_id: str,
        expected_version: int,
        mutate: Callable[[BaseModel | dict[str, Any]], None],
        *,
        media_object_id: str | None = None,
    ) -> StoredAsset:
        stored = self.workspace_assets.find_by_domain_id(
            context,
            None,
            asset_type,
            domain_id,
        )
        if stored is None:
            system_asset = self.system_assets.find_by_domain_id(
                context,
                asset_type,
                domain_id,
            )
            if system_asset is not None:
                raise AssetMutationForbiddenError("系统资产为只读资源")
            raise ScopedDocumentNotFoundError("资源不存在")
        self._ensure_version(stored, expected_version)
        document = copy.deepcopy(stored.document)
        mutate(document)
        return self.workspace_assets.update(
            context,
            None,
            stored.record_id,
            document,
            expected_version,
            media_object_id=media_object_id,
        )

    @staticmethod
    def _set_media_reference(
        document: BaseModel | dict[str, Any],
        media_id: str,
    ) -> None:
        if not isinstance(document, (Character, Scene, Prop)):
            raise DocumentPayloadValidationError("该资产不支持图片媒体")
        reference = f"media:{media_id}"
        document.image_url = reference
        if isinstance(document, Character):
            document.avatar_url = reference

    def attach_project_media(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        media_id: str,
        expected_version: int,
    ) -> StoredAsset:
        media = self.media.require(context, media_id)
        if media.project_id is not None and str(media.project_id) != project_id:
            raise ScopedDocumentNotFoundError("资源不存在")
        target, _repository, _parent_id = self._project_mutation_target(
            context,
            project_id,
            asset_type,
            domain_id,
        )
        if target.scope is AssetScope.SERIES and media.project_id is not None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return self._mutate_project_asset(
            context,
            project_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._set_media_reference(document, media_id),
            media_object_id=media_id,
        )

    def attach_series_media(
        self,
        context: WorkspaceContext,
        series_id: str,
        asset_type: str,
        domain_id: str,
        media_id: str,
        expected_version: int,
    ) -> StoredAsset:
        media = self.media.require(context, media_id)
        if media.project_id is not None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return self._mutate_series_asset(
            context,
            series_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._set_media_reference(document, media_id),
            media_object_id=media_id,
        )

    def attach_library_media(
        self,
        context: WorkspaceContext,
        asset_type: str,
        domain_id: str,
        media_id: str,
        expected_version: int,
    ) -> StoredAsset:
        media = self.media.require(context, media_id)
        if media.project_id is not None:
            raise ScopedDocumentNotFoundError("资源不存在")
        return self._mutate_library_asset(
            context,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._set_media_reference(document, media_id),
            media_object_id=media_id,
        )

    @staticmethod
    def _patch_document(
        document: BaseModel | dict[str, Any],
        attributes: Mapping[str, Any],
    ) -> None:
        forbidden = {
            "id",
            "status",
            "model",
            "provider",
            "scope",
            "image_url",
            "avatar_url",
            "full_body_image_url",
            "three_view_image_url",
            "headshot_image_url",
            "reference_sheet",
            "full_body",
            "three_views",
            "head_shot",
            "full_body_asset",
            "three_view_asset",
            "headshot_asset",
            "image_asset",
            "video_assets",
        }
        if isinstance(document, BaseModel):
            payload = document.model_dump(mode="json")
            allowed = set(type(document).model_fields) - forbidden
            payload.update(
                {key: value for key, value in attributes.items() if key in allowed}
            )
            validated = type(document).model_validate(payload)
            for key in allowed:
                setattr(document, key, getattr(validated, key))
            return
        for key, value in attributes.items():
            if key not in forbidden:
                document[key] = value

    @staticmethod
    def _toggle_field(document: BaseModel | dict[str, Any], field_name: str) -> None:
        if not isinstance(document, BaseModel) or field_name not in type(document).model_fields:
            raise DocumentPayloadValidationError("该资产不支持此操作")
        setattr(document, field_name, not bool(getattr(document, field_name)))

    def mutate_project_attributes(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        attributes: Mapping[str, Any],
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_project_asset(
            context,
            project_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._patch_document(document, attributes),
        )

    def mutate_series_attributes(
        self,
        context: WorkspaceContext,
        series_id: str,
        asset_type: str,
        domain_id: str,
        attributes: Mapping[str, Any],
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_series_asset(
            context,
            series_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._patch_document(document, attributes),
        )

    def mutate_library_attributes(
        self,
        context: WorkspaceContext,
        asset_type: str,
        domain_id: str,
        attributes: Mapping[str, Any],
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_library_asset(
            context,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._patch_document(document, attributes),
        )

    def toggle_project_field(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        field_name: str,
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_project_asset(
            context,
            project_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._toggle_field(document, field_name),
        )

    def toggle_series_field(
        self,
        context: WorkspaceContext,
        series_id: str,
        asset_type: str,
        domain_id: str,
        field_name: str,
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_series_asset(
            context,
            series_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._toggle_field(document, field_name),
        )

    def delete_project_asset(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        expected_version: int,
    ) -> None:
        stored = self.project_assets.find_by_domain_id(
            context,
            project_id,
            asset_type,
            domain_id,
        )
        if stored is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        self.project_assets.soft_delete(
            context,
            project_id,
            stored.record_id,
            expected_version,
        )

    def delete_series_asset(
        self,
        context: WorkspaceContext,
        series_id: str,
        asset_type: str,
        domain_id: str,
        expected_version: int,
    ) -> None:
        stored = self.series_assets.find_by_domain_id(
            context,
            series_id,
            asset_type,
            domain_id,
        )
        if stored is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        self.series_assets.soft_delete(
            context,
            series_id,
            stored.record_id,
            expected_version,
        )

    def delete_library_asset(
        self,
        context: WorkspaceContext,
        asset_type: str,
        domain_id: str,
        expected_version: int,
    ) -> None:
        stored = self.workspace_assets.find_by_domain_id(
            context,
            None,
            asset_type,
            domain_id,
        )
        if stored is None:
            system_asset = self.system_assets.find_by_domain_id(
                context,
                asset_type,
                domain_id,
            )
            if system_asset is not None:
                raise AssetMutationForbiddenError("系统资产为只读资源")
            raise ScopedDocumentNotFoundError("资源不存在")
        self.workspace_assets.soft_delete(
            context,
            None,
            stored.record_id,
            expected_version,
        )

    def promote_to_library(
        self,
        context: WorkspaceContext,
        *,
        source_kind: str,
        source_id: str,
        asset_type: str,
        domain_id: str,
    ) -> StoredAsset:
        with self.database.transaction(context.identity) as session:
            if source_kind == "project":
                source = self.project_assets.find_by_domain_id(
                    context,
                    source_id,
                    asset_type,
                    domain_id,
                    session=session,
                )
            elif source_kind == "series":
                source = self.series_assets.find_by_domain_id(
                    context,
                    source_id,
                    asset_type,
                    domain_id,
                    session=session,
                )
            else:
                raise DocumentPayloadValidationError("资产来源类型无效")
            if source is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            document = _document_with_new_id(source.document)
            return self.workspace_assets.add(
                context,
                None,
                asset_type,
                document,
                provenance={
                    "origin": "promoted",
                    "source_scope": source.scope.value,
                    "source_asset_record_id": source.record_id,
                    "source_domain_id": source.domain_id,
                },
                media_object_id=source.media_object_id,
                session=session,
            )

    def fork_to_project(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
    ) -> StoredAsset:
        with self.database.transaction(context.identity) as session:
            source = self.workspace_assets.find_by_domain_id(
                context,
                None,
                asset_type,
                domain_id,
                session=session,
            )
            if source is None:
                source = self.system_assets.find_by_domain_id(
                    context,
                    asset_type,
                    domain_id,
                    session=session,
                )
            if source is None:
                raise ScopedDocumentNotFoundError("资源不存在")
            document = _document_with_new_id(source.document)
            return self.project_assets.add(
                context,
                project_id,
                asset_type,
                document,
                provenance={
                    "origin": "forked",
                    "source_scope": source.scope.value,
                    "source_asset_record_id": source.record_id,
                    "source_domain_id": source.domain_id,
                },
                media_object_id=source.media_object_id,
                session=session,
            )

    def import_series_assets(
        self,
        context: WorkspaceContext,
        target_series_id: str,
        source_series_id: str,
        domain_ids: list[str],
    ) -> tuple[list[StoredAsset], list[str]]:
        imported: list[StoredAsset] = []
        skipped: list[str] = []
        with self.database.transaction(context.identity) as session:
            source_assets = {
                asset.domain_id: asset
                for asset in self.series_assets.list(
                    context,
                    source_series_id,
                    session=session,
                )
            }
            self.series_assets.list(
                context,
                target_series_id,
                session=session,
            )
            for domain_id in domain_ids:
                source = source_assets.get(domain_id)
                if source is None:
                    skipped.append(domain_id)
                    continue
                document = _document_with_new_id(source.document)
                imported.append(
                    self.series_assets.add(
                        context,
                        target_series_id,
                        source.asset_type,
                        document,
                        provenance={
                            "origin": "imported",
                            "source_scope": source.scope.value,
                            "source_asset_record_id": source.record_id,
                            "source_domain_id": source.domain_id,
                        },
                        media_object_id=source.media_object_id,
                        session=session,
                    )
                )
        return imported, skipped

    def add_custom_voice(
        self,
        context: WorkspaceContext,
        series_id: str,
        *,
        voice_id: str,
        label: str,
        origin: str,
        target_model: str,
        source_audio_url: str | None = None,
        voice_prompt: str | None = None,
    ) -> StoredAsset:
        voice = CustomVoice(
            id=voice_id,
            label=label,
            origin=origin,
            target_model=target_model,
            source_audio_url=source_audio_url,
            voice_prompt=voice_prompt,
            created_at=time.time(),
        )
        return self.series_assets.add(
            context,
            series_id,
            "voice",
            voice,
            provenance={"origin": f"voice_{origin}"},
        )

    def list_custom_voices(
        self,
        context: WorkspaceContext,
        series_id: str,
    ) -> list[StoredAsset]:
        return self.series_assets.list(
            context,
            series_id,
            asset_type="voice",
        )

    def delete_custom_voice(
        self,
        context: WorkspaceContext,
        series_id: str,
        voice_id: str,
        expected_version: int,
    ) -> None:
        stored = self.series_assets.find_by_domain_id(
            context,
            series_id,
            "voice",
            voice_id,
        )
        if stored is None:
            raise ScopedDocumentNotFoundError("资源不存在")
        self.series_assets.soft_delete(
            context,
            series_id,
            stored.record_id,
            expected_version,
        )

    def bind_character_voice(
        self,
        context: WorkspaceContext,
        project_id: str,
        character_id: str,
        voice_id: str,
        voice_name: str,
        expected_version: int,
    ) -> StoredAsset:
        return self.mutate_project_attributes(
            context,
            project_id,
            "character",
            character_id,
            {"voice_id": voice_id, "voice_name": voice_name},
            expected_version,
        )

    def update_character_voice_params(
        self,
        context: WorkspaceContext,
        project_id: str,
        character_id: str,
        *,
        speed: float,
        pitch: float,
        volume: int,
        expected_version: int,
    ) -> StoredAsset:
        return self.mutate_project_attributes(
            context,
            project_id,
            "character",
            character_id,
            {
                "voice_speed": speed,
                "voice_pitch": pitch,
                "voice_volume": volume,
            },
            expected_version,
        )

    @staticmethod
    def _variant_containers(
        document: BaseModel | dict[str, Any],
        generation_type: str | None,
    ) -> list[Any]:
        if isinstance(document, Character):
            containers = {
                "reference_sheet": document.reference_sheet,
                "full_body": document.full_body_asset,
                "three_view": document.three_view_asset,
                "headshot": document.headshot_asset,
            }
            if generation_type:
                return [containers.get(generation_type)]
            return [
                containers["reference_sheet"],
                containers["full_body"],
                containers["three_view"],
                containers["headshot"],
            ]
        if isinstance(document, (Scene, Prop)):
            return [document.image_asset]
        return []

    @staticmethod
    def _container_variants(container: Any) -> tuple[list[Any], str | None]:
        if container is None:
            return [], None
        if hasattr(container, "image_variants"):
            return container.image_variants, "selected_image_id"
        if hasattr(container, "variants"):
            return container.variants, "selected_id"
        return [], None

    @classmethod
    def _select_variant(
        cls,
        document: BaseModel | dict[str, Any],
        variant_id: str,
        generation_type: str | None,
    ) -> None:
        for container in cls._variant_containers(document, generation_type):
            variants, selected_field = cls._container_variants(container)
            variant = next((item for item in variants if item.id == variant_id), None)
            if variant is None or selected_field is None:
                continue
            setattr(container, selected_field, variant_id)
            if isinstance(document, Character):
                document.image_url = variant.url
                if generation_type == "headshot":
                    document.avatar_url = variant.url
            elif isinstance(document, (Scene, Prop)):
                document.image_url = variant.url
            return
        raise ScopedDocumentNotFoundError("资源不存在")

    @classmethod
    def _delete_variant(
        cls,
        document: BaseModel | dict[str, Any],
        variant_id: str,
    ) -> None:
        for container in cls._variant_containers(document, None):
            variants, selected_field = cls._container_variants(container)
            if not any(item.id == variant_id for item in variants):
                continue
            remaining = [item for item in variants if item.id != variant_id]
            if hasattr(container, "image_variants"):
                container.image_variants = remaining
            else:
                container.variants = remaining
            selected_id = getattr(container, selected_field) if selected_field else None
            if selected_id == variant_id and selected_field:
                setattr(container, selected_field, remaining[-1].id if remaining else None)
            selected_id = getattr(container, selected_field) if selected_field else None
            selected = next((item for item in remaining if item.id == selected_id), None)
            if isinstance(document, (Character, Scene, Prop)):
                document.image_url = selected.url if selected else None
            return
        raise ScopedDocumentNotFoundError("资源不存在")

    @classmethod
    def _favorite_variant(
        cls,
        document: BaseModel | dict[str, Any],
        variant_id: str,
        generation_type: str | None,
        is_favorited: bool,
    ) -> None:
        for container in cls._variant_containers(document, generation_type):
            variants, _selected_field = cls._container_variants(container)
            variant = next((item for item in variants if item.id == variant_id), None)
            if variant is not None:
                variant.is_favorited = is_favorited
                return
        raise ScopedDocumentNotFoundError("资源不存在")

    def select_project_variant(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        variant_id: str,
        generation_type: str | None,
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_project_asset(
            context,
            project_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._select_variant(
                document,
                variant_id,
                generation_type,
            ),
        )

    def delete_project_variant(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        variant_id: str,
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_project_asset(
            context,
            project_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._delete_variant(document, variant_id),
        )

    def favorite_project_variant(
        self,
        context: WorkspaceContext,
        project_id: str,
        asset_type: str,
        domain_id: str,
        variant_id: str,
        generation_type: str | None,
        is_favorited: bool,
        expected_version: int,
    ) -> StoredAsset:
        return self._mutate_project_asset(
            context,
            project_id,
            asset_type,
            domain_id,
            expected_version,
            lambda document: self._favorite_variant(
                document,
                variant_id,
                generation_type,
                is_favorited,
            ),
        )
