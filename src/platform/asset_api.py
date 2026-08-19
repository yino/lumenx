from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..audio.tts import VOICES
from .ai_gateway_api import AITaskSubmitter, CloudAIRequestAdapter
from .ai_request_policy import enforce_cloud_ai_request
from .asset_repositories import AssetConflictError, StoredAsset
from .asset_service import AssetMutationForbiddenError, CloudAssetService
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .content_repositories import (
    DocumentPayloadValidationError,
    InvalidRepositoryContextError,
    OptimisticVersionConflictError,
    ScopedDocumentNotFoundError,
)
from .contracts import MediaWrite, UserContext, WorkspaceContext
from .media_storage import CloudMediaStorage, MediaValidationError
from .identifiers import parse_database_id


AssetType = Literal["character", "scene", "prop"]


class AssetVersionRequiredError(ValueError):
    pass


class CreateScopedAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=240)
    description: str = ""
    persona: str = ""
    media_id: str | None = None
    voice_id: str | None = None
    age: str | None = None
    gender: str | None = None
    clothing: str | None = None
    time_of_day: str | None = None
    lighting_mood: str | None = None
    visual_weight: int | None = Field(default=None, ge=1, le=5)

    def service_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class CreateLibraryAssetRequest(CreateScopedAssetRequest):
    asset_type: AssetType

    def service_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude={"asset_type"}, exclude_none=True)


class AssetTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(min_length=1)
    asset_type: AssetType


class UpdateAssetImageRequest(AssetTargetRequest):
    media_id: str = Field(min_length=1)


class UpdateAssetAttributesRequest(AssetTargetRequest):
    attributes: dict[str, Any]


class UpdateAssetDescriptionRequest(AssetTargetRequest):
    description: str


class UpdateLibraryAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    persona: str | None = None
    media_id: str | None = None
    voice_id: str | None = None
    starred: bool | None = None
    locked: bool | None = None
    visual_weight: int | None = Field(default=None, ge=1, le=5)


class PromoteAssetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["project", "series"]
    source_id: str = Field(min_length=1)
    asset_type: AssetType
    asset_id: str = Field(min_length=1)


class ForkFromLibraryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: AssetType
    library_asset_id: str = Field(min_length=1)


class ImportAssetsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_series_id: str = Field(min_length=1)
    asset_ids: list[str] = Field(min_length=1)


class SelectVariantRequest(AssetTargetRequest):
    variant_id: str = Field(min_length=1)
    generation_type: str | None = None


class FavoriteVariantRequest(SelectVariantRequest):
    is_favorited: bool


class BindVoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str = Field(min_length=1)
    voice_name: str = Field(min_length=1, max_length=240)


class UpdateVoiceParamsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: int = Field(default=50, ge=0, le=100)


class SaveCustomVoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_id: str = Field(min_length=1)
    voice_id: str = Field(min_length=1)
    voice_prompt: str = Field(min_length=1, max_length=500)
    label: str = Field(min_length=1, max_length=240)


def _asset_response(stored: StoredAsset) -> dict[str, Any]:
    if isinstance(stored.document, BaseModel):
        payload = stored.document.model_dump(mode="json")
    else:
        payload = dict(stored.document)
    payload.update(
        {
            "asset_record_id": stored.record_id,
            "media_id": stored.media_object_id,
            "scope": stored.scope.value,
            "version": stored.version,
            "schema_version": stored.schema_version,
            "provenance": dict(stored.provenance),
        }
    )
    return payload


def _group_assets(assets: list[StoredAsset]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        "characters": [],
        "scenes": [],
        "props": [],
    }
    plural_names = {
        "character": "characters",
        "scene": "scenes",
        "prop": "props",
    }
    for asset in assets:
        key = plural_names.get(asset.asset_type)
        if key is not None:
            grouped[key].append(_asset_response(asset))
    return grouped


def _system_voice_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": meta["model_id"],
            "name": meta["name"],
            "gender": meta.get("gender", "Unknown"),
            "model": meta.get("model", "cosyvoice-v2"),
            "family": meta.get("family", "cosyvoice"),
            "supports_instruction": meta.get("supports_instruction", False),
            "dialect": meta.get("dialect"),
            "lang_primary": meta.get("lang_primary"),
            "origin": "system",
        }
        for meta in VOICES.values()
    ]


def _parse_expected_version(request: Request) -> int:
    raw_value = request.headers.get("if-match")
    if not raw_value:
        raise AssetVersionRequiredError("此操作需要 If-Match 资产版本")
    normalized = raw_value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    try:
        version = int(normalized)
    except ValueError as exc:
        raise AssetVersionRequiredError("If-Match 资产版本无效") from exc
    if version < 1:
        raise AssetVersionRequiredError("If-Match 资产版本无效")
    return version


def install_cloud_asset_api(
    app: FastAPI,
    auth: AuthApplication,
    *,
    media_storage: CloudMediaStorage | None = None,
    ai_submitter: AITaskSubmitter | None = None,
) -> CloudAssetService:
    service = CloudAssetService(auth.database)
    router = APIRouter(tags=["工作区资产"])
    ai_adapter = (
        CloudAIRequestAdapter(ai_submitter) if ai_submitter is not None else None
    )

    def require_principal(request: Request) -> SessionPrincipal:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf_token = (
            request.headers.get("x-csrf-token")
            if request.method in UNSAFE_METHODS
            else None
        )
        return auth.sessions.resolve(token, csrf_token=csrf_token)

    def require_context(
        request: Request,
        principal: SessionPrincipal = Depends(require_principal),
    ) -> WorkspaceContext:
        workspace_id = request.headers.get("x-workspace-id", "").strip()
        try:
            canonical_workspace_id = str(
                parse_database_id(workspace_id, field="工作区 ID")
            )
        except ValueError as exc:
            raise InvalidRepositoryContextError("请选择有效的工作区") from exc
        return WorkspaceContext(
            identity=UserContext(
                user_id=str(principal.user_id),
                session_id=str(principal.session_id),
            ),
            workspace_id=canonical_workspace_id,
        )

    @router.get("/voices")
    def list_system_voices(
        _context: WorkspaceContext = Depends(require_context),
    ) -> list[dict[str, Any]]:
        return _system_voice_catalog()

    @router.get("/projects/{project_id}/assets")
    def list_project_assets(
        project_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, list[dict[str, Any]]]:
        return _group_assets(service.list_project_assets(context, project_id))

    def create_project_asset(
        project_id: str,
        asset_type: AssetType,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext,
    ) -> dict[str, Any]:
        return _asset_response(
            service.create_project_asset(
                context,
                project_id,
                asset_type,
                **payload.service_payload(),
            )
        )

    @router.post("/projects/{project_id}/characters", status_code=201)
    def create_project_character(
        project_id: str,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return create_project_asset(project_id, "character", payload, context)

    @router.post("/projects/{project_id}/scenes", status_code=201)
    def create_project_scene(
        project_id: str,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return create_project_asset(project_id, "scene", payload, context)

    @router.post("/projects/{project_id}/props", status_code=201)
    def create_project_prop(
        project_id: str,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return create_project_asset(project_id, "prop", payload, context)

    def delete_project_asset(
        project_id: str,
        asset_type: AssetType,
        asset_id: str,
        request: Request,
        context: WorkspaceContext,
    ) -> dict[str, Any]:
        service.delete_project_asset(
            context,
            project_id,
            asset_type,
            asset_id,
            _parse_expected_version(request),
        )
        return {"status": "deleted", "asset_type": asset_type, "id": asset_id}

    @router.delete("/projects/{project_id}/characters/{asset_id}")
    def delete_project_character(
        project_id: str,
        asset_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return delete_project_asset(
            project_id, "character", asset_id, request, context
        )

    @router.delete("/projects/{project_id}/scenes/{asset_id}")
    def delete_project_scene(
        project_id: str,
        asset_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return delete_project_asset(project_id, "scene", asset_id, request, context)

    @router.delete("/projects/{project_id}/props/{asset_id}")
    def delete_project_prop(
        project_id: str,
        asset_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return delete_project_asset(project_id, "prop", asset_id, request, context)

    @router.post("/projects/{project_id}/assets/toggle_lock")
    def toggle_project_asset_lock(
        project_id: str,
        payload: AssetTargetRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.toggle_project_field(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                "locked",
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/assets/toggle_starred")
    def toggle_project_asset_starred(
        project_id: str,
        payload: AssetTargetRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.toggle_project_field(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                "starred",
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/assets/update_image")
    def update_project_asset_image(
        project_id: str,
        payload: UpdateAssetImageRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.attach_project_media(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                payload.media_id,
                _parse_expected_version(request),
            )
        )

    @router.post(
        "/projects/{project_id}/assets/{asset_type}/{asset_id}/upload"
    )
    def upload_project_asset_media(
        project_id: str,
        asset_type: AssetType,
        asset_id: str,
        request: Request,
        file: UploadFile = File(...),
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        if media_storage is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MEDIA_STORAGE_UNAVAILABLE",
                    "message": "云端媒体存储尚未启用",
                },
            )
        content = file.file.read(50 * 1024 * 1024 + 1)
        if len(content) > 50 * 1024 * 1024:
            raise MediaValidationError("单个媒体文件不能超过 50 MB")
        stored_media = media_storage.store(
            context,
            MediaWrite(
                content=content,
                content_type=file.content_type or "application/octet-stream",
                filename=file.filename or "upload.bin",
                project_id=project_id,
                provenance={
                    "origin": "asset_upload",
                    "asset_type": asset_type,
                    "asset_domain_id": asset_id,
                },
            ),
        )
        try:
            asset = service.attach_project_media(
                context,
                project_id,
                asset_type,
                asset_id,
                stored_media.media_id,
                _parse_expected_version(request),
            )
        except Exception:
            media_storage.delete(context, stored_media.media_id)
            raise
        return _asset_response(asset)

    @router.post("/projects/{project_id}/assets/update_attributes")
    def update_project_asset_attributes(
        project_id: str,
        payload: UpdateAssetAttributesRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.mutate_project_attributes(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                payload.attributes,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/assets/update_description")
    def update_project_asset_description(
        project_id: str,
        payload: UpdateAssetDescriptionRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.mutate_project_attributes(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                {"description": payload.description},
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/assets/variant/select")
    def select_project_variant(
        project_id: str,
        payload: SelectVariantRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.select_project_variant(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                payload.variant_id,
                payload.generation_type,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/assets/variant/delete")
    def delete_project_variant(
        project_id: str,
        payload: SelectVariantRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.delete_project_variant(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                payload.variant_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/projects/{project_id}/assets/variant/favorite")
    def favorite_project_variant(
        project_id: str,
        payload: FavoriteVariantRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.favorite_project_variant(
                context,
                project_id,
                payload.asset_type,
                payload.asset_id,
                payload.variant_id,
                payload.generation_type,
                payload.is_favorited,
                _parse_expected_version(request),
            )
        )

    @router.get("/series/{series_id}/assets")
    def list_series_assets(
        series_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, list[dict[str, Any]]]:
        return _group_assets(service.list_series_assets(context, series_id))

    def create_series_asset(
        series_id: str,
        asset_type: AssetType,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext,
    ) -> dict[str, Any]:
        return _asset_response(
            service.create_series_asset(
                context,
                series_id,
                asset_type,
                **payload.service_payload(),
            )
        )

    @router.post("/series/{series_id}/characters", status_code=201)
    def create_series_character(
        series_id: str,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return create_series_asset(series_id, "character", payload, context)

    @router.post("/series/{series_id}/scenes", status_code=201)
    def create_series_scene(
        series_id: str,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return create_series_asset(series_id, "scene", payload, context)

    @router.post("/series/{series_id}/props", status_code=201)
    def create_series_prop(
        series_id: str,
        payload: CreateScopedAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return create_series_asset(series_id, "prop", payload, context)

    @router.post("/series/{series_id}/assets/toggle_lock")
    def toggle_series_asset_lock(
        series_id: str,
        payload: AssetTargetRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.toggle_series_field(
                context,
                series_id,
                payload.asset_type,
                payload.asset_id,
                "locked",
                _parse_expected_version(request),
            )
        )

    @router.post("/series/{series_id}/assets/toggle_starred")
    def toggle_series_asset_starred(
        series_id: str,
        payload: AssetTargetRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.toggle_series_field(
                context,
                series_id,
                payload.asset_type,
                payload.asset_id,
                "starred",
                _parse_expected_version(request),
            )
        )

    @router.post("/series/{series_id}/assets/update_image")
    def update_series_asset_image(
        series_id: str,
        payload: UpdateAssetImageRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.attach_series_media(
                context,
                series_id,
                payload.asset_type,
                payload.asset_id,
                payload.media_id,
                _parse_expected_version(request),
            )
        )

    @router.post("/series/{series_id}/assets/update_attributes")
    def update_series_asset_attributes(
        series_id: str,
        payload: UpdateAssetAttributesRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.mutate_series_attributes(
                context,
                series_id,
                payload.asset_type,
                payload.asset_id,
                payload.attributes,
                _parse_expected_version(request),
            )
        )

    @router.delete("/series/{series_id}/assets/{asset_type}/{asset_id}")
    def delete_series_asset(
        series_id: str,
        asset_type: AssetType,
        asset_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        service.delete_series_asset(
            context,
            series_id,
            asset_type,
            asset_id,
            _parse_expected_version(request),
        )
        return {"status": "deleted", "asset_type": asset_type, "id": asset_id}

    @router.post("/series/{series_id}/assets/import")
    def import_series_assets(
        series_id: str,
        payload: ImportAssetsRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        imported, skipped = service.import_series_assets(
            context,
            series_id,
            payload.source_series_id,
            payload.asset_ids,
        )
        return {
            "imported": [_asset_response(asset) for asset in imported],
            "skipped_ids": skipped,
        }

    @router.get("/library/assets")
    def list_library_assets(
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, list[dict[str, Any]]]:
        return _group_assets(service.list_library_assets(context))

    @router.post("/library/assets", status_code=201)
    def create_library_asset(
        payload: CreateLibraryAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.create_library_asset(
                context,
                payload.asset_type,
                **payload.service_payload(),
            )
        )

    @router.api_route(
        "/library/assets/{asset_type}/{asset_id}",
        methods=["PUT", "PATCH"],
    )
    def update_library_asset(
        asset_type: AssetType,
        asset_id: str,
        payload: UpdateLibraryAssetRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        updates = payload.model_dump(exclude_unset=True)
        media_id = updates.pop("media_id", None)
        if media_id is not None:
            if updates:
                raise DocumentPayloadValidationError(
                    "媒体更新不能与其他资产字段同时提交"
                )
            return _asset_response(
                service.attach_library_media(
                    context,
                    asset_type,
                    asset_id,
                    media_id,
                    _parse_expected_version(request),
                )
            )
        return _asset_response(
            service.mutate_library_attributes(
                context,
                asset_type,
                asset_id,
                updates,
                _parse_expected_version(request),
            )
        )

    @router.delete("/library/assets/{asset_type}/{asset_id}")
    def delete_library_asset(
        asset_type: AssetType,
        asset_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        service.delete_library_asset(
            context,
            asset_type,
            asset_id,
            _parse_expected_version(request),
        )
        return {"status": "deleted", "asset_type": asset_type, "id": asset_id}

    @router.post("/library/assets/promote", status_code=201)
    def promote_asset_to_library(
        payload: PromoteAssetRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.promote_to_library(
                context,
                source_kind=payload.source_kind,
                source_id=payload.source_id,
                asset_type=payload.asset_type,
                domain_id=payload.asset_id,
            )
        )

    @router.post("/projects/{project_id}/assets/fork_from_library", status_code=201)
    def fork_asset_from_library(
        project_id: str,
        payload: ForkFromLibraryRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.fork_to_project(
                context,
                project_id,
                payload.asset_type,
                payload.library_asset_id,
            )
        )

    @router.post("/projects/{project_id}/characters/{character_id}/voice")
    def bind_character_voice(
        project_id: str,
        character_id: str,
        payload: BindVoiceRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.bind_character_voice(
                context,
                project_id,
                character_id,
                payload.voice_id,
                payload.voice_name,
                _parse_expected_version(request),
            )
        )

    @router.put("/projects/{project_id}/characters/{character_id}/voice_params")
    def update_character_voice_params(
        project_id: str,
        character_id: str,
        payload: UpdateVoiceParamsRequest,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.update_character_voice_params(
                context,
                project_id,
                character_id,
                speed=payload.speed,
                pitch=payload.pitch,
                volume=payload.volume,
                expected_version=_parse_expected_version(request),
            )
        )

    @router.get("/series/{series_id}/custom_voices")
    def list_series_custom_voices(
        series_id: str,
        context: WorkspaceContext = Depends(require_context),
    ) -> list[dict[str, Any]]:
        return [
            _asset_response(asset)
            for asset in service.list_custom_voices(context, series_id)
        ]

    @router.delete("/series/{series_id}/custom_voices/{voice_id}")
    def delete_series_custom_voice(
        series_id: str,
        voice_id: str,
        request: Request,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, bool]:
        service.delete_custom_voice(
            context,
            series_id,
            voice_id,
            _parse_expected_version(request),
        )
        return {"removed": True}

    @router.post("/voice/design/accept", status_code=201)
    def save_designed_voice(
        payload: SaveCustomVoiceRequest,
        context: WorkspaceContext = Depends(require_context),
    ) -> dict[str, Any]:
        return _asset_response(
            service.add_custom_voice(
                context,
                payload.series_id,
                voice_id=payload.voice_id,
                label=payload.label,
                origin="design",
                target_model="cosyvoice-v3.5-plus",
                voice_prompt=payload.voice_prompt,
            )
        )

    async def gateway_not_ready(
        request: Request,
        _context: WorkspaceContext = Depends(require_context),
    ) -> None:
        await enforce_cloud_ai_request(request)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AI_GATEWAY_NOT_READY",
                "message": "云端 AI 网关尚未启用，未执行也未扣除算力券",
            },
        )

    def add_ai_route(path: str, capability: str, operation: str) -> None:
        if ai_adapter is None:
            endpoint = gateway_not_ready
        else:
            async def endpoint(
                request: Request,
                context: WorkspaceContext = Depends(require_context),
            ) -> JSONResponse:
                return await ai_adapter.submit_request(
                    request,
                    context,
                    capability=capability,
                    operation=operation,
                )

        router.add_api_route(
            path,
            endpoint,
            methods=["POST"],
            name=f"cloud_ai_{operation.replace('.', '_')}",
        )

    add_ai_route(
        "/series/{series_id}/assets/generate",
        "image.t2i",
        "series.assets.generate",
    )
    add_ai_route(
        "/projects/{project_id}/assets/generate",
        "image.t2i",
        "project.asset.generate",
    )
    add_ai_route(
        "/projects/{project_id}/generate_assets",
        "image.t2i",
        "project.assets.generate_all",
    )
    add_ai_route(
        "/projects/{project_id}/assets/generate_motion_ref",
        "video.i2v",
        "project.asset.generate_motion_reference",
    )
    add_ai_route(
        "/projects/{project_id}/assets/{asset_type}/{asset_id}/generate_video",
        "video.i2v",
        "project.asset.generate_video",
    )
    add_ai_route("/voice/preview", "speech.tts", "voice.preview")
    add_ai_route("/voice/clone", "speech.tts", "voice.clone")
    add_ai_route("/voice/design/preview", "speech.tts", "voice.design.preview")
    add_ai_route(
        "/voice/design/translate",
        "prompt.polish",
        "voice.design.translate",
    )
    if ai_adapter is None:
        router.add_api_route(
            "/library/assets/upload",
            gateway_not_ready,
            methods=["POST"],
        )

    app.include_router(router)

    @app.exception_handler(AssetVersionRequiredError)
    def handle_asset_version_required(
        _request: Request,
        exc: AssetVersionRequiredError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=428,
            content={"code": "ASSET_VERSION_REQUIRED", "message": str(exc)},
        )

    @app.exception_handler(AssetMutationForbiddenError)
    def handle_asset_mutation_forbidden(
        _request: Request,
        exc: AssetMutationForbiddenError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"code": "ASSET_MUTATION_FORBIDDEN", "message": str(exc)},
        )

    @app.exception_handler(AssetConflictError)
    def handle_asset_conflict(
        _request: Request,
        exc: AssetConflictError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "ASSET_CONFLICT", "message": str(exc)},
        )

    @app.exception_handler(ScopedDocumentNotFoundError)
    def handle_asset_not_found(
        _request: Request,
        _exc: ScopedDocumentNotFoundError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"code": "CONTENT_NOT_FOUND", "message": "资源不存在"},
        )

    @app.exception_handler(OptimisticVersionConflictError)
    def handle_asset_version_conflict(
        _request: Request,
        exc: OptimisticVersionConflictError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": "CONTENT_VERSION_CONFLICT", "message": str(exc)},
        )

    @app.exception_handler(InvalidRepositoryContextError)
    def handle_invalid_asset_context(
        _request: Request,
        exc: InvalidRepositoryContextError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "WORKSPACE_CONTEXT_INVALID", "message": str(exc)},
        )

    @app.exception_handler(DocumentPayloadValidationError)
    def handle_invalid_asset(
        _request: Request,
        exc: DocumentPayloadValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "CONTENT_INVALID", "message": str(exc)},
        )

    app.state.cloud_asset_service = service
    return service
