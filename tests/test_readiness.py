from contextlib import contextmanager
from pathlib import Path

import pytest

from src.platform import readiness
from src.platform.readiness import check_readiness
from src.platform.settings import (
    DeploymentSettings,
    ObjectStoreAdapter,
    ProviderAdapter,
)


def test_desktop_readiness_has_no_cloud_dependency_checks() -> None:
    result = check_readiness(DeploymentSettings(_env_file=None))

    assert result.ready is True
    assert result.checks == {"desktop": True}


@pytest.mark.parametrize(
    ("database_role_safe", "expected_ready"),
    [(True, True), (False, False)],
)
def test_cloud_readiness_rejects_database_roles_that_bypass_rls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_role_safe: bool,
    expected_ready: bool,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "server-secret")

    class Connection:
        def scalar(self, statement):
            return database_role_safe if "rolsuper" in str(statement) else 1

    class Engine:
        @contextmanager
        def connect(self):
            yield Connection()

        def dispose(self):
            pass

    class RedisClient:
        def ping(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(readiness, "create_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(readiness.Redis, "from_url", lambda *args, **kwargs: RedisClient())
    settings = DeploymentSettings(
        _env_file=None,
        deployment_mode="cloud",
        database_url="postgresql+psycopg://lumenx_app:secret@postgres/lumenx",
        redis_url="redis://redis:6379/0",
        oss_endpoint="oss.example.invalid",
        oss_bucket_name="private-bucket",
        oss_access_key_id="access-id",
        oss_access_key_secret="access-secret",
        session_secret="s" * 32,
        model_catalog_path=catalog,
        provider_secret_refs="DASHSCOPE_API_KEY",
        local_import_root=tmp_path,
    )

    result = check_readiness(settings)

    assert result.ready is expected_ready
    assert result.checks["database_role"] is database_role_safe


def test_release_test_readiness_accepts_guarded_deterministic_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")

    class Connection:
        def scalar(self, statement):
            return True if "rolsuper" in str(statement) else 1

    class Engine:
        @contextmanager
        def connect(self):
            yield Connection()

        def dispose(self):
            pass

    class RedisClient:
        def ping(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(readiness, "create_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(
        readiness.Redis,
        "from_url",
        lambda *args, **kwargs: RedisClient(),
    )
    settings = DeploymentSettings(
        _env_file=None,
        deployment_mode="test",
        test_adapters_enabled=True,
        object_store_adapter=ObjectStoreAdapter.DETERMINISTIC,
        provider_adapter=ProviderAdapter.DETERMINISTIC,
        test_object_store_root=tmp_path,
        test_signing_secret="t" * 32,
        database_url="postgresql+psycopg://lumenx_app:secret@postgres/lumenx",
        redis_url="redis://redis:6379/0",
        session_secret="s" * 32,
        model_catalog_path=catalog,
        local_import_root=tmp_path,
    )

    result = check_readiness(settings)

    assert result.ready is True
    assert result.checks["provider_secrets"] is True
