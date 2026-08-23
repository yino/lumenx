from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from src.platform.composition import compose_deployment_adapters
from src.platform.configuration_service import ConfigurationService
from src.platform.content_repositories import PostgresProjectRepository, PostgresSeriesRepository
from src.platform.contracts import (
    BillingService,
    CredentialProvider,
    IdentityContextProvider,
    MediaStorage,
    ModelConfigurationProvider,
    ScopedRepository,
    TaskDispatcher,
    UserContext,
)
from src.platform.desktop_adapters import (
    DesktopMediaStorage,
    InProcessTaskDispatcher,
    NoOpDesktopBillingService,
)
from src.platform.media_storage import CloudMediaStorage
from src.platform.ticket_reservation import TicketReservationService
from src.platform.settings import DeploymentMode, DeploymentSettings
from tests.test_content_repositories import RepositoryDatabase


def test_desktop_composition_selects_complete_local_adapter_set() -> None:
    settings = DeploymentSettings(_env_file=None, deployment_mode="desktop")
    adapters = compose_deployment_adapters(settings)

    assert adapters.mode is DeploymentMode.DESKTOP
    assert adapters.legacy_pipeline_enabled
    assert adapters.local_static_files_enabled
    assert not adapters.cloud_authentication_required
    assert adapters.billing.mode == "disabled"
    assert isinstance(adapters.identity(), IdentityContextProvider)
    assert isinstance(adapters.repositories.projects(), ScopedRepository)
    assert isinstance(adapters.repositories.series(), ScopedRepository)
    assert isinstance(adapters.media(), MediaStorage)
    assert isinstance(adapters.credentials({"DASHSCOPE_API_KEY": "local"}), CredentialProvider)
    assert isinstance(adapters.model_configuration(), ModelConfigurationProvider)
    assert isinstance(adapters.tasks(lambda _task_id: None), TaskDispatcher)
    assert isinstance(adapters.billing.service(), BillingService)
    assert adapters.billing.service is NoOpDesktopBillingService


def test_cloud_composition_selects_database_private_media_and_required_billing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "server-secret")
    settings = DeploymentSettings(
        _env_file=None,
        deployment_mode="cloud",
        database_url="postgresql+psycopg://lumenx:test@postgres/lumenx",
        redis_url="redis://redis:6379/0",
        oss_endpoint="oss.example.invalid",
        oss_bucket_name="private-bucket",
        oss_access_key_id="access-id",
        oss_access_key_secret="access-secret",
        session_secret="s" * 32,
        model_catalog_path=catalog,
        provider_secret_refs="DASHSCOPE_API_KEY",
    )
    adapters = compose_deployment_adapters(settings)

    assert adapters.mode is DeploymentMode.CLOUD
    assert not adapters.legacy_pipeline_enabled
    assert not adapters.local_static_files_enabled
    assert adapters.cloud_authentication_required
    assert adapters.identity is None
    assert adapters.billing.mode == "required"
    assert adapters.billing.reservation is not None
    assert adapters.billing.settlement is not None
    assert adapters.billing.retry is not None
    assert adapters.repositories.projects is PostgresProjectRepository
    assert adapters.repositories.series is PostgresSeriesRepository
    assert adapters.media is not DesktopMediaStorage
    assert adapters.tasks is not InProcessTaskDispatcher
    assert adapters.billing.service is TicketReservationService

    database = RepositoryDatabase()
    try:
        project_repository = adapters.repositories.projects(database)
        series_repository = adapters.repositories.series(database)
        model_configuration = adapters.model_configuration(
            database,
            UserContext(user_id="00000000-0000-0000-0000-000000000001"),
        )
        media_storage = adapters.media(database)
        assert isinstance(project_repository, ScopedRepository)
        assert isinstance(series_repository, ScopedRepository)
        assert isinstance(media_storage, CloudMediaStorage)
        assert isinstance(model_configuration.configuration, ConfigurationService)
        assert isinstance(adapters.credentials(), CredentialProvider)
    finally:
        database.engine.dispose()


def test_compose_uses_distinct_admin_and_rls_application_database_roles() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "LUMENX_DATABASE_USER: ${LUMENX_DATABASE_USER:-lumenx_app}" in compose
    assert "LUMENX_DATABASE_PASSWORD_FILE: /run/secrets/postgres_app_password" in compose
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in compose
    assert "LUMENX_REGISTRATION_EMERGENCY_DISABLED:-true" in compose
    assert "LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED:-true" in compose
    assert "LUMENX_PROVIDER_ACCOUNT_ID: ${LUMENX_PROVIDER_ACCOUNT_ID:-}" in compose
    assert "LUMENX_DATABASE_RESOURCE_ID: ${LUMENX_DATABASE_RESOURCE_ID:-}" in compose
    assert "LUMENX_REDIS_RESOURCE_ID: ${LUMENX_REDIS_RESOURCE_ID:-}" in compose
    assert "database-role-bootstrap:" in compose
    assert "registration-mode-bootstrap:" in compose
    assert "ai-tasks-bootstrap:" in Path("docker-compose.override.yml").read_text(encoding="utf-8")
    assert "scripts/set_local_ai_mode.py" in Path("docker-compose.override.yml").read_text(encoding="utf-8")
    assert 'LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED: "false"' in Path("docker-compose.override.yml").read_text(encoding="utf-8")
    assert "LUMENX_BOOTSTRAP_REGISTRATION_MODE" in compose
    assert "scripts/set_registration_mode.py" in compose
    assert "dockerfile: docker/Dockerfile.postgres-tools" in compose
    postgres_tools = Path("docker/Dockerfile.postgres-tools").read_text(encoding="utf-8")
    assert not Path("Dockerfile.postgres-tools").exists()
    assert "COPY docker/postgres-init-app-role.sh" in postgres_tools
    assert "COPY docker/postgres-backup.sh" in postgres_tools
    assert "condition: service_completed_successfully" in compose
    assert "lumenx-cloud-entrypoint celery -A src.platform.worker:celery_app inspect ping" in compose
    assert '"http://127.0.0.1/"' in compose


def test_makefile_exposes_explicit_sole_admin_adoption_recovery() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert "docker-admin-adopt:" in makefile
    assert "--adopt-existing-sole-admin" in makefile
    assert "$(COMPOSE) run --rm migration" in makefile
    assert "docker-admin-adopt  一次性接管唯一的异名管理员" in makefile


def test_local_compose_automatically_adopts_only_the_sole_legacy_admin() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    local_override = Path("docker-compose.override.yml").read_text(encoding="utf-8")
    release_override = Path("docker-compose.release.yml").read_text(encoding="utf-8")

    assert 'LUMENX_BOOTSTRAP_ADMIN_ADOPT_EXISTING_SOLE: "false"' in compose
    assert 'LUMENX_BOOTSTRAP_ADMIN_ADOPT_EXISTING_SOLE: "true"' in local_override
    assert "LUMENX_ALLOW_DEFAULT_BOOTSTRAP_ADMIN_PASSWORD: \"true\"" in local_override
    assert "LUMENX_BOOTSTRAP_ADMIN_ADOPT_EXISTING_SOLE" not in release_override


def test_local_ai_bootstrap_is_explicitly_guarded_and_production_stays_closed() -> None:
    script = Path("scripts/set_local_ai_mode.py").read_text(encoding="utf-8")
    local_override = Path("docker-compose.override.yml").read_text(encoding="utf-8")
    production = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "LUMENX_LOCAL_DOCKER" in script
    assert "LUMENX_LOCAL_AI_BOOTSTRAP_ENABLED" in script
    assert "configuration.local_ai_bootstrap" in script
    assert 'LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED: "false"' in local_override
    assert (
        "LUMENX_PROVIDER_SECRET_REFS: DASHSCOPE_API_KEY,ARK_API_KEY,XLINKS_API_KEY"
        in local_override
    )
    assert "XLINKS_API_KEY: ${LUMENX_LOCAL_XLINKS_API_KEY:" in local_override
    assert "ARK_API_KEY: ${LUMENX_LOCAL_ARK_API_KEY:" in local_override
    assert "SEEDANCE_PROVIDER_MODE: ark" in local_override
    assert 'AICapability.VIDEO_I2V: "seedance-2.0-i2v"' in script
    assert 'AICapability.VIDEO_R2V: "seedance-2.0-r2v"' in script
    assert "LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED:-true" in production
    assert "ai-tasks-bootstrap:" not in production


def test_frontend_image_fails_when_dependency_install_is_incomplete() -> None:
    dockerfile = Path("docker/Dockerfile.frontend").read_text(encoding="utf-8")
    lockfile = Path("frontend/package-lock.json").read_text(encoding="utf-8")

    assert not Path("Dockerfile.frontend").exists()
    assert "npm install --global npm@10.9.4" in dockerfile
    assert "npm ci --no-audit --no-fund" in dockerfile
    assert "test -x node_modules/.bin/next" in dockerfile
    assert "registry.anpm.alibaba-inc.com" not in lockfile


def test_backend_image_always_contains_database_migrations() -> None:
    dockerfile = Path("docker/Dockerfile.backend").read_text(encoding="utf-8")

    assert "COPY migrations/ migrations/" in dockerfile
    assert "COPY alembic.ini alembic.ini" in dockerfile


def test_release_smoke_runs_as_module_from_application_root() -> None:
    release_compose = Path("docker-compose.release.yml").read_text(encoding="utf-8")
    release_smoke = Path("scripts/cloud_release_smoke.py").read_text(encoding="utf-8")

    assert "dockerfile: docker/Dockerfile.backend" in release_compose
    assert 'command: ["python", "-m", "scripts.cloud_release_smoke"]' in release_compose
    assert 'command: ["python", "scripts/cloud_release_smoke.py"]' not in release_compose
    assert "LUMENX_BOOTSTRAP_ADMIN_USERNAME: releaseadmin" in release_compose
    assert "PlatformAdminBootstrapService" not in release_smoke
    assert "uuid.UUID" not in release_smoke
    assert "/api/v1/admin/recharge-orders" in release_smoke
    assert "/api/v1/admin/system-scenes" in release_smoke
    assert "/api/v1/system-scenes/{scene_id}/copy" in release_smoke


def test_postgres_migration_verifier_runs_directly_from_project_root() -> None:
    environment = os.environ.copy()
    environment.pop("LUMENX_DATABASE_URL", None)
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "scripts/verify_postgres_migrations.py"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "迁移验证缺少 PostgreSQL 管理连接" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_release_nginx_syntax_check_does_not_require_a_running_backend() -> None:
    workflow = Path(".github/workflows/cloud-release-check.yml").read_text(
        encoding="utf-8"
    )

    assert '--entrypoint /bin/sh frontend -c' in workflow
    assert 'printf "127.0.0.1 backend\\n" >> /etc/hosts' in workflow
    assert "exec /docker-entrypoint.sh nginx -t" in workflow
    assert "run --rm --no-deps frontend nginx -t" not in workflow


def test_makefile_exposes_safe_development_and_build_entrypoints() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")

    assert ".DEFAULT_GOAL := help" in makefile
    for target in (
        "help",
        "doctor",
        "install",
        "dev",
        "backend",
        "frontend",
        "build",
        "build-mac",
        "build-windows",
        "test",
        "check",
        "migrate",
        "docker-config",
        "docker-build",
        "docker-up",
        "docker-down",
        "docker-logs",
        "release-check",
    ):
        assert f"{target}:" in makefile

    assert "docker-up: docker-config" in makefile
    assert "$(COMPOSE) up -d --build" in makefile
    assert "\t./build_mac.sh" in makefile
    assert (
        "\tpowershell -NoProfile -ExecutionPolicy Bypass "
        "-File ./build_windows.ps1"
    ) in makefile


def test_makefile_release_check_has_no_paid_or_rollout_side_effects() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    release_target = next(
        line for line in makefile.splitlines() if line.startswith("release-check:")
    )
    dependencies = set(release_target.partition(":")[2].split())

    assert {
        "check",
        "build",
        "docker-production-config",
        "openspec-validate",
    } <= dependencies
    for forbidden_command in (
        "staging_cloud_canary",
        "confirm-paid-call",
        "legacy_api_compat_evidence",
        "LUMENX_LEGACY_API_COMPAT",
    ):
        assert forbidden_command not in makefile


def test_local_compose_uses_env_values_and_docker_managed_volumes() -> None:
    override = Path("docker-compose.override.yml").read_text(encoding="utf-8")
    production = Path("docker-compose.yml").read_text(encoding="utf-8")
    makefile = Path("Makefile").read_text(encoding="utf-8")
    production_postgres = production.split("  postgres:", 1)[1].split(
        "\n  redis:", 1
    )[0]

    assert "name: lumenx-local" in override
    assert "secrets: !reset {}" in override
    assert override.count("secrets: !reset []") == 11
    assert "local-output:/app/output" in override
    assert "local-imports:/imports:ro" in override
    assert '"127.0.0.1:3000:80"' in override
    assert '"127.0.0.1:15433:5432"' in override
    assert override.count("pull_policy: build") == 11
    assert 'LUMENX_REGISTRATION_EMERGENCY_DISABLED: "false"' in override
    assert "LUMENX_BOOTSTRAP_REGISTRATION_MODE:-open" in override
    assert "\n    ports:" not in production_postgres
    assert "./output" not in override
    assert "./imports" not in override
    assert "./secrets" not in override
    assert "docker-env: docker-env" not in makefile
    assert "docker-config: docker-env" in makefile
    assert (
        "$(COMPOSE) -f docker-compose.yml -f docker-compose.release.yml config --quiet"
        in makefile
    )

    backup = Path("docker/postgres-backup.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then' in backup
    assert 'PGPASSWORD="${POSTGRES_PASSWORD:?缺少 PostgreSQL 备份密码}"' in backup


def test_local_docker_env_setup_never_prints_secret_values() -> None:
    setup = Path("scripts/setup-local-docker-env.sh").read_text(encoding="utf-8")

    assert "umask 077" in setup
    assert "chmod 600" in setup
    assert "openssl rand -hex 32" in setup
    assert "未输出任何凭据" in setup
    assert "LUMENX_LOCAL_ARK_API_KEY" in setup
    assert "LUMENX_LOCAL_ARK_BASE_URL" in setup
    assert "LUMENX_LOCAL_ARK_SEEDANCE_MODEL" in setup
    assert "set -x" not in setup
