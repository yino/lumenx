from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .configuration_schemas import (
    AICapability,
    ConfigurationDraft,
    ModelRouteConfig,
    PlatformConfig,
)
from .configuration_service import ConfigurationService, StoredConfiguration
from .contracts import UserContext
from .database import Database
from .db_models import AuditEventRecord, UserRecord
from .identifiers import parse_database_id
from .settings import DeploymentMode, get_deployment_settings


CAPABILITY_MAP = {
    "t2i": AICapability.IMAGE_T2I,
    "i2i": AICapability.IMAGE_I2I,
    "t2v": AICapability.VIDEO_T2V,
    "i2v": AICapability.VIDEO_I2V,
    "r2v": AICapability.VIDEO_R2V,
    "v2v": AICapability.VIDEO_V2V,
}


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


class ModelCatalogSeeder:
    def __init__(self, database: Database, catalog_path: Path) -> None:
        self.database = database
        self.catalog_path = catalog_path
        self.configuration = ConfigurationService(database)

    def _load_catalog(self) -> tuple[dict[str, Any], str]:
        raw = self.catalog_path.read_bytes()
        try:
            catalog = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("模型目录不是有效 JSON") from exc
        if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), dict):
            raise ValueError("模型目录缺少 models 对象")
        canonical = json.dumps(
            catalog,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return catalog, hashlib.sha256(canonical).hexdigest()

    def _require_database_admin(self, admin: UserContext) -> None:
        self.configuration._require_admin(admin)
        with self.database.transaction(admin) as session:
            is_admin = session.scalar(
                select(UserRecord.is_platform_admin).where(
                    UserRecord.id
                    == parse_database_id(admin.user_id, field="管理员 ID")
                )
            )
        if not is_admin:
            raise PermissionError("指定用户不是平台管理员")

    def _find_existing(
        self,
        admin: UserContext,
        fingerprint: str,
    ) -> StoredConfiguration | None:
        with self.database.transaction(admin) as session:
            events = list(
                session.scalars(
                    select(AuditEventRecord).where(
                        AuditEventRecord.action == "configuration.seed"
                    )
                )
            )
        for event in events:
            summary = event.after_summary or {}
            if summary.get("catalog_fingerprint") != fingerprint or not event.target_id:
                continue
            try:
                return self.configuration.get_version(admin, event.target_id)
            except Exception:
                continue
        return None

    @staticmethod
    def _secret_ref(model: dict[str, Any]) -> str:
        sources = model.get("credential_sources") or {}
        backend = model.get("default_backend")
        candidates = sources.get(backend) if backend else None
        if not candidates:
            candidates = next((value for value in sources.values() if value), None)
        if not candidates:
            raise ValueError(f"模型 {model.get('id')} 缺少服务端凭据引用")
        return str(candidates[0])

    @staticmethod
    def _parameter_contract(
        model: dict[str, Any],
        capability: AICapability,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        defaults: dict[str, Any] = {}
        params = dict(model.get("params") or {})
        for raw_name, metadata in params.items():
            name = _snake_case(raw_name)
            if metadata is False or metadata is None:
                continue
            rule: dict[str, Any] = {"name": name, "required": False}
            if isinstance(metadata, dict):
                default = metadata.get("default")
                choices = metadata.get("options")
                sample = default
                if sample is None and isinstance(choices, list) and choices:
                    sample = choices[0]
                if isinstance(sample, bool):
                    rule["value_type"] = "boolean"
                elif isinstance(sample, int):
                    rule["value_type"] = "integer"
                elif isinstance(sample, float):
                    rule["value_type"] = "number"
                else:
                    rule["value_type"] = "string"
                if metadata.get("min") is not None:
                    rule["minimum"] = metadata["min"]
                if metadata.get("max") is not None:
                    rule["maximum"] = metadata["max"]
                if isinstance(choices, list) and choices:
                    rule["choices"] = choices
                if default is not None:
                    defaults[name] = default
            elif name in {
                "watermark",
                "prompt_extend",
                "generate_audio",
                "audio",
                "sound",
                "vidu_audio",
            }:
                rule["value_type"] = "boolean"
                defaults[name] = False
            elif name == "seed":
                rule.update(
                    {"value_type": "integer", "minimum": 0, "maximum": 2147483647}
                )
            else:
                rule["value_type"] = "string"
            rules.append(rule)

        duration = model.get("duration")
        rule_names = {rule["name"] for rule in rules}
        if (
            capability.value.startswith("video.")
            and isinstance(duration, dict)
            and "duration" not in rule_names
        ):
            duration_rule: dict[str, Any] = {
                "name": "duration",
                "value_type": "integer",
                "required": False,
            }
            fixed_value = duration.get("value")
            choices = duration.get("options")
            if fixed_value is not None:
                duration_rule["choices"] = [fixed_value]
                defaults["duration"] = fixed_value
            elif isinstance(choices, list) and choices:
                duration_rule["choices"] = choices
                defaults["duration"] = duration.get("default", choices[0])
            else:
                duration_rule["minimum"] = duration.get("min", 1)
                duration_rule["maximum"] = duration.get("max", 60)
                if duration.get("default") is not None:
                    defaults["duration"] = duration["default"]
            rules.append(duration_rule)
        batch_name = "count" if capability.value.startswith("image.") else "output_count"
        if batch_name not in {rule["name"] for rule in rules}:
            rules.append(
                {
                    "name": batch_name,
                    "value_type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                }
            )
            defaults[batch_name] = 1
        return rules, defaults

    @staticmethod
    def _metering_formula(
        model: dict[str, Any],
        capability: AICapability,
    ) -> dict[str, Any]:
        params = model.get("params") or {}
        resolution_config = params.get("resolution") or params.get("size") or {}
        resolutions = (
            resolution_config.get("options", [])
            if isinstance(resolution_config, dict)
            else []
        )
        if not resolutions:
            resolutions = ["default"]
        multipliers = {str(resolution): 1 for resolution in resolutions}
        if capability.value.startswith("image."):
            return {
                "kind": "image",
                "review_required": True,
                "base_tokens": 0,
                "per_image_tokens": 1,
                "max_images": 4,
                "resolution_multipliers": multipliers,
            }
        duration = model.get("duration") or {}
        max_duration = 60
        if isinstance(duration, dict):
            if duration.get("value") is not None:
                max_duration = duration["value"]
            elif isinstance(duration.get("options"), list) and duration["options"]:
                max_duration = max(duration["options"])
            elif duration.get("max") is not None:
                max_duration = duration["max"]
        return {
            "kind": "video",
            "review_required": True,
            "base_tokens": 0,
            "tokens_per_second": 1,
            "max_duration_seconds": max_duration,
            "max_outputs": 4,
            "resolution_multipliers": multipliers,
            "audio_multiplier": 1,
        }

    @classmethod
    def _route(
        cls,
        model: dict[str, Any],
        capability: AICapability,
    ) -> ModelRouteConfig:
        rules, defaults = cls._parameter_contract(model, capability)
        backend = str(model.get("default_backend") or model.get("provider") or "provider")
        display_name = str(model.get("display_name") or model["id"])
        return ModelRouteConfig.model_validate(
            {
                "capability": capability,
                "display_name_zh": f"{display_name}（目录导入）",
                "provider": backend,
                "provider_model_id": model["id"],
                "enabled": False,
                "is_primary": False,
                "priority": max(0, int((model.get("ui") or {}).get("order", 100))),
                "default_parameters": defaults,
                "parameter_schema": rules,
                "metering_formula": cls._metering_formula(model, capability),
                "fallback_policy": {"enabled": False},
                "secret_ref": cls._secret_ref(model),
            }
        )

    def seed(
        self,
        admin: UserContext,
        platform: PlatformConfig,
        *,
        reason: str,
    ) -> tuple[StoredConfiguration, bool]:
        self._require_database_admin(admin)
        catalog, fingerprint = self._load_catalog()
        existing = self._find_existing(admin, fingerprint)
        if existing is not None:
            return existing, False

        routes: list[ModelRouteConfig] = []
        capabilities: set[AICapability] = set()
        for model_id, raw_model in sorted(catalog["models"].items()):
            model = dict(raw_model)
            model.setdefault("id", model_id)
            for raw_capability in model.get("capabilities", []):
                capability = CAPABILITY_MAP.get(str(raw_capability))
                if capability is None:
                    continue
                capabilities.add(capability)
                routes.append(self._route(model, capability))
        if not routes:
            raise ValueError("模型目录中没有可导入的能力路由")

        platform_payload = platform.model_dump(mode="json")
        platform_payload["exposed_capabilities"] = sorted(
            capability.value for capability in capabilities
        )
        draft = ConfigurationDraft(
            reason=reason,
            platform=PlatformConfig.model_validate(platform_payload),
            routes=routes,
        )
        stored = self.configuration.create_version(
            admin,
            draft,
            audit_action="configuration.seed",
            audit_metadata={
                "catalog_fingerprint": fingerprint,
                "catalog_version": catalog.get("version"),
            },
        )
        return stored, True


def main() -> None:
    parser = argparse.ArgumentParser(description="导入云端模型目录草稿")
    parser.add_argument("--admin-user-id", required=True)
    parser.add_argument("--catalog")
    parser.add_argument("--tokens-per-ticket", type=int, default=1000)
    parser.add_argument("--reason", default="从仓库模型目录生成初始配置草稿")
    args = parser.parse_args()

    settings = get_deployment_settings()
    if settings.deployment_mode is not DeploymentMode.CLOUD or not settings.database_url:
        raise RuntimeError("模型目录导入仅支持已配置数据库的云端模式")
    catalog_path = Path(args.catalog) if args.catalog else settings.model_catalog_path
    if catalog_path is None:
        raise RuntimeError("缺少模型目录路径")
    database = Database(settings.database_url)
    try:
        platform = PlatformConfig(
            tokens_per_ticket=args.tokens_per_ticket,
            registration_initial_grant_microtickets=(
                settings.registration_initial_grant_microtickets
            ),
            session_idle_seconds=settings.session_idle_seconds,
            session_absolute_seconds=settings.session_absolute_seconds,
            max_sessions_per_user=5,
            max_ai_concurrency_per_user=2,
            exposed_capabilities=[AICapability.IMAGE_T2I],
        )
        stored, created = ModelCatalogSeeder(database, catalog_path).seed(
            UserContext(
                user_id=args.admin_user_id,
                session_id="catalog-seeder",
                is_platform_admin=True,
            ),
            platform,
            reason=args.reason,
        )
        state = "已创建" if created else "已存在"
        print(f"模型配置草稿{state}：版本 {stored.version_number}，ID {stored.id}")
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
