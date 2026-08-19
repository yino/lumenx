#!/usr/bin/env python3
"""Enable the server-side AI routes used by the local Compose studio flow.

The production configuration deliberately starts with AI task creation disabled.
This script is only wired into ``docker-compose.override.yml`` and creates a new
versioned configuration instead of mutating the active version in place.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from src.platform.configuration_schemas import (
    AICapability,
    ModelRouteConfig,
)
from src.platform.configuration_service import ConfigurationService
from src.platform.contracts import AdminContext, SystemContext
from src.platform.database import Database
from src.platform.db_models import AdminUserRecord
from src.platform.model_catalog_seeder import ModelCatalogSeeder
from src.platform.settings import DeploymentMode, get_deployment_settings


TEXT_CAPABILITIES = (AICapability.SCRIPT_ANALYSIS, AICapability.PROMPT_POLISH)
DEFAULT_TTS_MODEL = "cosyvoice-v2"
CATALOG_ROUTE_MODELS = {
    AICapability.IMAGE_T2I: "wan2.7-image-pro",
    AICapability.IMAGE_I2I: "wan2.7-image-pro",
    AICapability.VIDEO_I2V: "seedance-2.0-i2v",
    AICapability.VIDEO_R2V: "seedance-2.0-r2v",
}


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _default_route(capability: AICapability, model_id: str) -> ModelRouteConfig:
    display_name = {
        AICapability.SCRIPT_ANALYSIS: "本地剧本分析模型",
        AICapability.PROMPT_POLISH: "本地提示词润色模型",
    }[capability]
    return ModelRouteConfig.model_validate(
        {
            "capability": capability,
            "display_name_zh": display_name,
            "provider": "dashscope",
            "provider_model_id": model_id,
            "enabled": True,
            "is_primary": True,
            "priority": 10,
            "default_parameters": {},
            "parameter_schema": [],
            "metering_formula": {
                "kind": "llm",
                "input_weight": 1,
                "output_weight": 1,
                "max_input_tokens": 32768,
                "max_output_tokens": 8192,
            },
            "fallback_policy": {"enabled": False},
            "secret_ref": "DASHSCOPE_API_KEY",
        }
    )


def _speech_route(model_id: str = DEFAULT_TTS_MODEL) -> ModelRouteConfig:
    return ModelRouteConfig.model_validate(
        {
            "capability": AICapability.SPEECH_TTS,
            "display_name_zh": "本地对白语音模型",
            "provider": "dashscope",
            "provider_model_id": model_id,
            "enabled": True,
            "is_primary": True,
            "priority": 10,
            "default_parameters": {},
            "parameter_schema": [],
            "metering_formula": {
                "kind": "speech",
                "unit": "characters",
                "tokens_per_unit": 1,
                "max_units": 20000,
            },
            "fallback_policy": {"enabled": False},
            "secret_ref": "DASHSCOPE_API_KEY",
        }
    )


def _catalog_route(
    capability: AICapability,
    model_id: str,
    catalog: dict,
) -> ModelRouteConfig:
    raw_model = catalog.get("models", {}).get(model_id)
    if not isinstance(raw_model, dict):
        raise RuntimeError(f"本地 AI 模型目录缺少 {model_id}")
    model = dict(raw_model)
    model.setdefault("id", model_id)
    route = ModelCatalogSeeder._route(model, capability)
    return route.model_copy(
        update={
            "display_name_zh": f"{route.display_name_zh.removesuffix('（目录导入）')}（本地）",
            "enabled": True,
            "is_primary": True,
            "metering_formula": route.metering_formula.model_copy(
                update={"review_required": False}
            ),
        }
    )


def _replace_primary_route(
    routes: list[ModelRouteConfig],
    desired: ModelRouteConfig,
) -> list[ModelRouteConfig]:
    updated: list[ModelRouteConfig] = []
    inserted = False
    for route in routes:
        if route.capability is not desired.capability:
            updated.append(route)
            continue
        if (
            not inserted
            and route.provider == desired.provider
            and route.provider_model_id == desired.provider_model_id
        ):
            updated.append(desired)
            inserted = True
            continue
        updated.append(route.model_copy(update={"enabled": False, "is_primary": False}))
    if not inserted:
        updated.append(desired)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="为本地 Compose 启用服务端 AI")
    parser.add_argument(
        "--reason",
        default="本地 Docker 初始化服务端 AI 路由",
        help="写入配置和审计记录的原因",
    )
    args = parser.parse_args()

    if not _env_true("LUMENX_LOCAL_DOCKER") or not _env_true(
        "LUMENX_LOCAL_AI_BOOTSTRAP_ENABLED"
    ):
        raise RuntimeError("本地 AI 配置引导仅允许由本地 Docker Compose 显式启用")

    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.CLOUD or not settings.database_url:
        raise RuntimeError("本地 AI 配置引导需要使用带 PostgreSQL 的云端部署模式")

    model_id = os.getenv("LUMENX_LOCAL_AI_MODEL", "qwen3.6-plus").strip()
    if not model_id:
        raise RuntimeError("LUMENX_LOCAL_AI_MODEL 不能为空")

    database = Database(settings.database_url)
    try:
        with database.transaction(SystemContext(service_name="local-ai-bootstrap")) as session:
            admin = session.scalar(
                select(AdminUserRecord)
                .where(AdminUserRecord.status == "active")
                .order_by(AdminUserRecord.created_at.asc(), AdminUserRecord.id.asc())
                .limit(1)
            )
        if admin is None:
            raise RuntimeError("没有可用于发布本地 AI 配置的有效平台管理员")

        identity = AdminContext(
            admin_id=str(admin.id),
            session_id="local-ai-bootstrap",
            username=admin.username,
        )
        service = ConfigurationService(database, verification_provider_available=False)
        active = service.get_active(identity)
        catalog_path = settings.model_catalog_path
        if catalog_path is None or not catalog_path.is_file():
            raise RuntimeError("本地 AI 配置引导缺少生成后的模型目录")
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

        routes = list(active.draft.routes)
        capabilities = set(active.draft.platform.exposed_capabilities)
        desired_routes = [
            *(_default_route(capability, model_id) for capability in TEXT_CAPABILITIES),
            _speech_route(
                os.getenv("LUMENX_LOCAL_TTS_MODEL", DEFAULT_TTS_MODEL).strip()
                or DEFAULT_TTS_MODEL
            ),
            *(
                _catalog_route(capability, catalog_model_id, catalog)
                for capability, catalog_model_id in CATALOG_ROUTE_MODELS.items()
            ),
        ]
        for desired in desired_routes:
            routes = _replace_primary_route(routes, desired)
            capabilities.add(desired.capability)

        feature_flags = active.draft.platform.feature_flags
        if not feature_flags.new_ai_tasks_enabled:
            feature_flags = feature_flags.model_copy(update={"new_ai_tasks_enabled": True})
        changed = (
            routes != list(active.draft.routes)
            or capabilities != set(active.draft.platform.exposed_capabilities)
            or feature_flags != active.draft.platform.feature_flags
        )
        if not changed:
            print(
                json.dumps(
                    {
                        "changed": False,
                        "config_version_id": active.id,
                        "new_ai_tasks_enabled": True,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        platform = active.draft.platform.model_copy(
            update={
                "exposed_capabilities": sorted(
                    capabilities, key=lambda capability: capability.value
                ),
                "feature_flags": feature_flags,
            }
        )
        draft = active.draft.model_copy(
            update={
                "reason": args.reason.strip(),
                "platform": platform,
                "routes": routes,
            }
        )
        created = service.create_version(
            identity,
            draft,
            audit_action="configuration.local_ai_bootstrap",
            audit_metadata={
                "text_model_id": model_id,
                "tts_model_id": next(
                    route.provider_model_id
                    for route in desired_routes
                    if route.capability is AICapability.SPEECH_TTS
                ),
                "catalog_route_models": {
                    capability.value: catalog_model_id
                    for capability, catalog_model_id in CATALOG_ROUTE_MODELS.items()
                },
            },
        )
        activated = service.activate_version(
            identity,
            created.id,
            reason=args.reason,
        )
        print(
            json.dumps(
                {
                    "changed": True,
                    "config_version_id": activated.id,
                    "version_number": activated.version_number,
                    "model_id": model_id,
                    "capabilities": [route.capability.value for route in desired_routes],
                    "new_ai_tasks_enabled": True,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
