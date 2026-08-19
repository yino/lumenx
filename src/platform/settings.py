from __future__ import annotations

import os
import re
from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentMode(str, Enum):
    DESKTOP = "desktop"
    CLOUD = "cloud"
    TEST = "test"


class ObjectStoreAdapter(str, Enum):
    OSS = "oss"
    DETERMINISTIC = "deterministic"


class ProviderAdapter(str, Enum):
    PRODUCTION = "production"
    DETERMINISTIC = "deterministic"


class DeploymentSettings(BaseSettings):
    """Validated startup settings shared by deployment composition code."""

    model_config = SettingsConfigDict(
        env_prefix="LUMENX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    deployment_mode: DeploymentMode = DeploymentMode.DESKTOP
    desktop_build: bool = False
    test_adapters_enabled: bool = False
    object_store_adapter: ObjectStoreAdapter = ObjectStoreAdapter.OSS
    provider_adapter: ProviderAdapter = ProviderAdapter.PRODUCTION
    test_object_store_root: Path = Path("/tmp/lumenx-release-objects")
    test_signing_secret: SecretStr | None = None
    provider_output_root: Path = Path("output/provider-results")

    database_url: str | None = None
    redis_url: str | None = None
    database_resource_id: str | None = None
    redis_resource_id: str | None = None

    oss_endpoint: str | None = None
    oss_bucket_name: str | None = None
    oss_access_key_id: str | None = None
    oss_access_key_secret: SecretStr | None = None
    oss_private: bool = True

    session_secret: SecretStr | None = None
    model_catalog_path: Path | None = None
    provider_secret_refs: str | None = None
    provider_account_id: str | None = None
    local_import_root: Path = Path("imports")
    local_import_max_bytes: int = 10 * 1024 * 1024 * 1024

    allowed_origins: str = "http://localhost:3000,http://localhost:3008"
    auth_rate_window_seconds: int = 15 * 60
    auth_registration_limit: int = 5
    auth_login_limit: int = 10
    auth_reset_limit: int = 5
    admin_rate_window_seconds: int = 60
    admin_mutation_limit: int = 120
    admin_sensitive_read_limit: int = 60
    admin_export_limit: int = 20
    session_idle_seconds: int = 7 * 24 * 60 * 60
    session_absolute_seconds: int = 30 * 24 * 60 * 60
    registration_initial_grant_microtickets: int = 0
    registration_emergency_disabled: bool = False
    new_ai_tasks_emergency_disabled: bool = False
    admin_console_enabled: bool = True
    admin_console_mutations_enabled: bool = True
    global_worker_concurrency: int = 8

    @property
    def provider_secret_ref_names(self) -> tuple[str, ...]:
        if not self.provider_secret_refs:
            return ()
        return tuple(ref.strip() for ref in self.provider_secret_refs.split(",") if ref.strip())

    @property
    def allowed_origin_set(self) -> frozenset[str]:
        return frozenset(
            origin.strip().rstrip("/")
            for origin in self.allowed_origins.split(",")
            if origin.strip()
        )

    @model_validator(mode="after")
    def validate_cloud_prerequisites(self) -> "DeploymentSettings":
        if self.desktop_build and self.deployment_mode is not DeploymentMode.DESKTOP:
            raise ValueError("桌面构建产物禁止启用云端部署模式")
        if self.deployment_mode is DeploymentMode.DESKTOP:
            if self.test_adapters_enabled or (
                self.object_store_adapter is ObjectStoreAdapter.DETERMINISTIC
                or self.provider_adapter is ProviderAdapter.DETERMINISTIC
            ):
                raise ValueError("桌面模式禁止启用发布测试适配器")
            return self

        deterministic_requested = (
            self.object_store_adapter is ObjectStoreAdapter.DETERMINISTIC
            or self.provider_adapter is ProviderAdapter.DETERMINISTIC
        )
        if self.deployment_mode is DeploymentMode.CLOUD and (
            self.test_adapters_enabled or deterministic_requested
        ):
            raise ValueError("正常云端模式禁止启用确定性测试适配器")
        if self.deployment_mode is DeploymentMode.TEST:
            if not self.test_adapters_enabled:
                raise ValueError("发布测试模式必须显式启用测试适配器")
            if (
                self.object_store_adapter is not ObjectStoreAdapter.DETERMINISTIC
                or self.provider_adapter is not ProviderAdapter.DETERMINISTIC
            ):
                raise ValueError("发布测试模式必须同时使用确定性供应商和对象存储")
            if (
                self.test_signing_secret is None
                or len(self.test_signing_secret.get_secret_value()) < 32
            ):
                raise ValueError("发布测试签名密钥至少需要 32 个字符")

        required = {
            "LUMENX_DATABASE_URL": self.database_url,
            "LUMENX_REDIS_URL": self.redis_url,
            "LUMENX_SESSION_SECRET": self.session_secret,
            "LUMENX_MODEL_CATALOG_PATH": self.model_catalog_path,
        }
        if self.deployment_mode is DeploymentMode.CLOUD:
            required.update(
                {
                    "LUMENX_OSS_ENDPOINT": self.oss_endpoint,
                    "LUMENX_OSS_BUCKET_NAME": self.oss_bucket_name,
                    "LUMENX_OSS_ACCESS_KEY_ID": self.oss_access_key_id,
                    "LUMENX_OSS_ACCESS_KEY_SECRET": self.oss_access_key_secret,
                    "LUMENX_PROVIDER_SECRET_REFS": self.provider_secret_refs,
                }
            )
        missing = [name for name, value in required.items() if value is None or value == ""]
        if missing:
            raise ValueError(f"云端启动配置不完整：缺少 {', '.join(missing)}")

        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("LUMENX_DATABASE_URL 必须使用 PostgreSQL")
        if not self.redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("LUMENX_REDIS_URL 必须使用 Redis 协议")
        if self.deployment_mode is DeploymentMode.CLOUD and not self.oss_private:
            raise ValueError("云端对象存储必须配置为私有")

        if len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("LUMENX_SESSION_SECRET 至少需要 32 个字符")
        if not self.model_catalog_path.is_file():
            raise ValueError("LUMENX_MODEL_CATALOG_PATH 必须指向可读取的模型目录文件")
        if not self.local_import_root.is_dir():
            raise ValueError("LUMENX_LOCAL_IMPORT_ROOT 必须指向可读取的导入根目录")
        if self.local_import_max_bytes <= 0:
            raise ValueError("LUMENX_LOCAL_IMPORT_MAX_BYTES 必须为正整数")
        if not self.allowed_origin_set:
            raise ValueError("云端模式必须配置至少一个允许的前端来源")
        rate_values = (
            self.auth_rate_window_seconds,
            self.auth_registration_limit,
            self.auth_login_limit,
            self.auth_reset_limit,
            self.admin_rate_window_seconds,
            self.admin_mutation_limit,
            self.admin_sensitive_read_limit,
            self.admin_export_limit,
            self.session_idle_seconds,
            self.session_absolute_seconds,
            self.global_worker_concurrency,
        )
        if any(value <= 0 for value in rate_values):
            raise ValueError("认证限流配置必须为正整数")
        if self.session_absolute_seconds < self.session_idle_seconds:
            raise ValueError("会话绝对有效期不能短于闲置有效期")
        if self.registration_initial_grant_microtickets < 0:
            raise ValueError("注册初始赠送算力券不能为负数")

        invalid_refs = [
            ref
            for ref in self.provider_secret_ref_names
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", ref)
        ]
        if invalid_refs:
            raise ValueError("服务端凭据引用必须是大写环境变量名")

        unresolved_refs = [
            ref
            for ref in self.provider_secret_ref_names
            if not os.getenv(ref)
        ]
        if unresolved_refs:
            raise ValueError(f"服务端凭据引用未解析：{', '.join(unresolved_refs)}")
        return self


@lru_cache(maxsize=1)
def get_deployment_settings() -> DeploymentSettings:
    return DeploymentSettings()
