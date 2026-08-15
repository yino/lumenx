from __future__ import annotations

from dataclasses import dataclass

from redis import Redis
from sqlalchemy import create_engine, text

from .settings import DeploymentMode, DeploymentSettings, ProviderAdapter


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    checks: dict[str, bool]


def check_readiness(settings: DeploymentSettings) -> ReadinessResult:
    if settings.deployment_mode is DeploymentMode.DESKTOP:
        return ReadinessResult(ready=True, checks={"desktop": True})

    deterministic_provider_ready = (
        settings.deployment_mode is DeploymentMode.TEST
        and settings.test_adapters_enabled
        and settings.provider_adapter is ProviderAdapter.DETERMINISTIC
    )
    checks = {
        "postgresql": False,
        "database_role": False,
        "redis": False,
        "private_oss": settings.oss_private,
        "model_catalog": bool(
            settings.model_catalog_path and settings.model_catalog_path.is_file()
        ),
        "provider_secrets": bool(settings.provider_secret_ref_names)
        or deterministic_provider_ready,
    }

    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            checks["postgresql"] = connection.scalar(text("SELECT 1")) == 1
            checks["database_role"] = bool(
                connection.scalar(
                    text(
                        """
                        SELECT NOT rolsuper AND NOT rolbypassrls
                        FROM pg_roles
                        WHERE rolname = current_user
                        """
                    )
                )
            )
    finally:
        engine.dispose()

    redis_client = Redis.from_url(settings.redis_url)
    try:
        checks["redis"] = bool(redis_client.ping())
    finally:
        redis_client.close()

    return ReadinessResult(ready=all(checks.values()), checks=checks)
