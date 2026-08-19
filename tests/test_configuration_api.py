from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.platform.auth.admin import AdminAuthorizationError
from src.platform.auth.admin_identity import AdminSessionPrincipal
from src.platform.auth.sessions import SessionAuthenticationError
from src.platform.configuration_api import (
    _deployment_resource_fingerprints,
    install_cloud_configuration_api,
)
from src.platform.db_models import (
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
)
from src.platform.settings import DeploymentMode, ObjectStoreAdapter, ProviderAdapter
from tests.test_configuration_schemas import _image_route, _platform
from tests.test_content_repositories import RepositoryDatabase, _create_scope


@pytest.fixture
def configuration_client():
    database = RepositoryDatabase()
    ConfigVersionRecord.__table__.create(database.engine)
    ModelConfigRecord.__table__.create(database.engine)
    PlatformConfigRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)
    sessions = Mock()
    sessions.resolve.return_value = AdminSessionPrincipal(
        admin_id=9001,
        session_id=9101,
        username="admin",
        must_change_password=False,
    )
    app = FastAPI()
    install_cloud_configuration_api(
        app,
        SimpleNamespace(database=database, admin_sessions=sessions),
        SimpleNamespace(
            global_worker_concurrency=3,
            deployment_mode=DeploymentMode.CLOUD,
            object_store_adapter=ObjectStoreAdapter.OSS,
            provider_adapter=ProviderAdapter.PRODUCTION,
            test_adapters_enabled=False,
            oss_private=True,
            registration_emergency_disabled=True,
            new_ai_tasks_emergency_disabled=True,
            database_url=(
                "postgresql+psycopg://lumenx_app:database-secret@postgres:5432/lumenx"
            ),
            redis_url="redis://:redis-secret@redis:6379/0",
            database_resource_id="staging-postgres-volume/lumenx",
            redis_resource_id="staging-redis-volume",
            oss_endpoint="https://oss-cn-beijing.aliyuncs.com",
            oss_bucket_name="lumenx-staging-private",
            provider_account_id="dashscope-staging-subaccount",
        ),
    )

    @app.exception_handler(AdminAuthorizationError)
    def handle_admin_denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(
            status_code=403,
            content={"code": "ADMIN_REQUIRED", "message": str(exc)},
        )

    @app.exception_handler(SessionAuthenticationError)
    def handle_admin_unauthenticated(_request: Request, exc: SessionAuthenticationError):
        return JSONResponse(status_code=401, content={"code": exc.code, "message": str(exc)})

    client = TestClient(app)
    client.cookies.set("lumenx_admin_session", "admin-session")
    client.headers.update({"x-csrf-token": "admin-csrf"})
    yield client, sessions
    database.engine.dispose()


def _draft_payload(reason: str, *, provider_model_id: str = "wan-image-v1") -> dict:
    return {
        "reason": reason,
        "platform": _platform(),
        "routes": [_image_route(provider_model_id=provider_model_id)],
    }


def test_admin_configuration_api_supports_draft_validate_activate_and_inspect(
    configuration_client,
) -> None:
    client, sessions = configuration_client

    created = client.post(
        "/admin/configuration/versions",
        json=_draft_payload("创建首个配置版本"),
    )
    assert created.status_code == 201
    version = created.json()
    assert version["status"] == "draft"
    assert version["platform"]["tokens_per_ticket"] == 1000
    assert version["routes"][0]["display_name_zh"] == "平台图像模型"

    validation = client.post(
        f"/admin/configuration/versions/{version['id']}/validate"
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is True

    activated = client.post(
        f"/admin/configuration/versions/{version['id']}/activate",
        json={"reason": "校验通过，正式启用"},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"

    active = client.get("/admin/configuration/active")
    versions = client.get("/admin/configuration/versions")
    detail = client.get(f"/admin/configuration/versions/{version['id']}")
    assert active.json()["id"] == version["id"]
    assert versions.json()[0]["id"] == version["id"]
    assert detail.json()["routes"][0]["provider_model_id"] == "wan-image-v1"
    assert sessions.resolve.call_count >= 6


def test_admin_configuration_api_requires_reason_to_disable_draft(
    configuration_client,
) -> None:
    client, _sessions = configuration_client
    created = client.post(
        "/admin/configuration/versions",
        json=_draft_payload("创建待停用草稿", provider_model_id="wan-image-unused"),
    ).json()

    missing_reason = client.post(
        f"/admin/configuration/versions/{created['id']}/disable",
        json={"reason": ""},
    )
    disabled = client.post(
        f"/admin/configuration/versions/{created['id']}/disable",
        json={"reason": "该模型暂不开放"},
    )

    assert missing_reason.status_code == 422
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"


def test_configuration_api_denies_normal_user_without_disclosing_routes(
    configuration_client,
) -> None:
    client, sessions = configuration_client
    client.cookies.delete("lumenx_admin_session")
    client.cookies.set("lumenx_session", "normal-user-session")

    response = client.get("/admin/configuration/versions")

    assert response.status_code == 401
    assert response.json()["code"] == "ADMIN_AUTH_REQUIRED"
    assert "secret_ref" not in response.text


def test_deployment_state_exposes_release_gates_without_secret_material(
    configuration_client,
) -> None:
    client, _sessions = configuration_client

    response = client.get("/admin/configuration/deployment-state")

    assert response.status_code == 200
    assert response.json() == {
        "worker_concurrency": 3,
        "authority": "environment_or_compose",
        "activation_required": True,
        "deployment_mode": "cloud",
        "object_store_adapter": "oss",
        "provider_adapter": "production",
        "test_adapters_enabled": False,
        "oss_private": True,
        "registration_emergency_disabled": True,
        "new_ai_tasks_emergency_disabled": True,
        "resource_fingerprints": {
            "postgresql": response.json()["resource_fingerprints"]["postgresql"],
            "redis": response.json()["resource_fingerprints"]["redis"],
            "oss_bucket": response.json()["resource_fingerprints"]["oss_bucket"],
            "provider_account": response.json()["resource_fingerprints"][
                "provider_account"
            ],
        },
    }
    assert all(
        fingerprint.startswith("sha256:") and len(fingerprint) == 71
        for fingerprint in response.json()["resource_fingerprints"].values()
    )
    for sensitive_value in (
        "lumenx_app:database-secret@postgres",
        "lumenx-staging-private",
        "oss-cn-beijing.aliyuncs.com",
        "dashscope-staging-subaccount",
        "database-secret",
        "redis-secret",
    ):
        assert sensitive_value not in response.text


def test_deployment_resource_fingerprints_ignore_credentials_and_url_noise() -> None:
    original = SimpleNamespace(
        database_url=(
            "postgresql+psycopg://lumenx_app:first-secret@DB.EXAMPLE:5432/lumenx"
            "?sslmode=require"
        ),
        redis_url="rediss://default:first-secret@REDIS.EXAMPLE:6380/2?ssl=true",
        database_resource_id="staging-postgres-volume/lumenx",
        redis_resource_id="staging-redis-volume",
        oss_endpoint="https://OSS-CN-BEIJING.ALIYUNCS.COM/",
        oss_bucket_name="LumenX-Staging-Private",
        provider_account_id="dashscope-staging-subaccount",
    )
    rotated_credentials = SimpleNamespace(
        database_url=(
            "postgresql://other_role:second-secret@postgres-internal:5432/lumenx"
            "?application_name=lumenx"
        ),
        redis_url="redis://other:second-secret@redis-internal:6379/9",
        database_resource_id="staging-postgres-volume/lumenx",
        redis_resource_id="staging-redis-volume",
        oss_endpoint="oss-cn-beijing.aliyuncs.com",
        oss_bucket_name="lumenx-staging-private",
        provider_account_id="dashscope-staging-subaccount",
    )

    assert _deployment_resource_fingerprints(
        original
    ) == _deployment_resource_fingerprints(rotated_credentials)

    different_resources = SimpleNamespace(
        **{
            **vars(rotated_credentials),
            "database_resource_id": "production-postgres-volume/lumenx",
            "redis_resource_id": "production-redis-volume",
        }
    )
    original_fingerprints = _deployment_resource_fingerprints(original)
    different_fingerprints = _deployment_resource_fingerprints(different_resources)
    assert original_fingerprints["postgresql"] != different_fingerprints["postgresql"]
    assert original_fingerprints["redis"] != different_fingerprints["redis"]
