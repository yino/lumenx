from pathlib import Path

import pytest
from pydantic import ValidationError

from src.platform.settings import (
    DeploymentMode,
    DeploymentSettings,
    ObjectStoreAdapter,
    ProviderAdapter,
)


def test_desktop_settings_do_not_require_cloud_dependencies() -> None:
    settings = DeploymentSettings(_env_file=None)

    assert settings.deployment_mode is DeploymentMode.DESKTOP


def test_emergency_feature_flags_default_open_and_parse_explicit_shutdown() -> None:
    defaults = DeploymentSettings(_env_file=None)
    shutdown = DeploymentSettings(
        _env_file=None,
        registration_emergency_disabled="true",
        new_ai_tasks_emergency_disabled="1",
    )

    assert defaults.registration_emergency_disabled is False
    assert defaults.new_ai_tasks_emergency_disabled is False
    assert shutdown.registration_emergency_disabled is True
    assert shutdown.new_ai_tasks_emergency_disabled is True


def test_cloud_settings_reject_missing_prerequisites() -> None:
    with pytest.raises(ValidationError, match="LUMENX_DATABASE_URL"):
        DeploymentSettings(_env_file=None, deployment_mode="cloud")


def test_cloud_settings_accept_complete_private_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_path = tmp_path / "model_catalog.json"
    catalog_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "server-only-secret")

    settings = DeploymentSettings(
        _env_file=None,
        deployment_mode="cloud",
        database_url="postgresql+psycopg://lumenx:secret@postgres/lumenx",
        redis_url="redis://redis:6379/0",
        database_resource_id="staging-postgres-volume/lumenx",
        redis_resource_id="staging-redis-volume",
        oss_endpoint="oss-cn-beijing.aliyuncs.com",
        oss_bucket_name="lumenx-private",
        oss_access_key_id="access-key-id",
        oss_access_key_secret="access-key-secret",
        session_secret="a" * 32,
        model_catalog_path=catalog_path,
        provider_secret_refs="DASHSCOPE_API_KEY",
        provider_account_id="dashscope-staging-subaccount",
    )

    assert settings.deployment_mode is DeploymentMode.CLOUD
    assert settings.provider_secret_ref_names == ("DASHSCOPE_API_KEY",)
    assert settings.database_resource_id == "staging-postgres-volume/lumenx"
    assert settings.redis_resource_id == "staging-redis-volume"
    assert settings.provider_account_id == "dashscope-staging-subaccount"


def test_normal_cloud_rejects_deterministic_release_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_path = tmp_path / "model_catalog.json"
    catalog_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "server-only-secret")

    with pytest.raises(ValidationError, match="正常云端模式禁止"):
        DeploymentSettings(
            _env_file=None,
            deployment_mode="cloud",
            test_adapters_enabled=True,
            object_store_adapter="deterministic",
            provider_adapter="deterministic",
            test_signing_secret="t" * 32,
            database_url="postgresql+psycopg://lumenx:secret@postgres/lumenx",
            redis_url="redis://redis:6379/0",
            session_secret="s" * 32,
            model_catalog_path=catalog_path,
        )


def test_release_test_mode_requires_both_deterministic_adapters(tmp_path: Path) -> None:
    catalog_path = tmp_path / "model_catalog.json"
    catalog_path.write_text("{}", encoding="utf-8")
    common = {
        "_env_file": None,
        "deployment_mode": "test",
        "test_adapters_enabled": True,
        "test_signing_secret": "t" * 32,
        "database_url": "postgresql+psycopg://lumenx:secret@postgres/lumenx",
        "redis_url": "redis://redis:6379/0",
        "session_secret": "s" * 32,
        "model_catalog_path": catalog_path,
    }
    with pytest.raises(ValidationError, match="必须同时使用"):
        DeploymentSettings(**common)

    settings = DeploymentSettings(
        **common,
        object_store_adapter="deterministic",
        provider_adapter="deterministic",
        test_object_store_root=tmp_path / "objects",
    )

    assert settings.deployment_mode is DeploymentMode.TEST
    assert settings.object_store_adapter is ObjectStoreAdapter.DETERMINISTIC
    assert settings.provider_adapter is ProviderAdapter.DETERMINISTIC
