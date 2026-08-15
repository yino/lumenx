from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth.admin import AdminAuthorizationError
from .auth.api import AuthApplication
from .auth.protection import UNSAFE_METHODS
from .auth.sessions import SessionAuthenticationError, SessionPrincipal
from .configuration_schemas import ConfigurationDraft
from .configuration_service import (
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    ConfigurationService,
    ConfigurationValidationError,
    StoredConfiguration,
)
from .contracts import UserContext
from .settings import DeploymentSettings


class ConfigurationReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


def _sha256_fingerprint(identity: dict[str, str] | None) -> str | None:
    if identity is None or any(not value for value in identity.values()):
        return None
    canonical = json.dumps(
        identity,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _url_resource_identity(
    value: str | None,
    *,
    default_port: int,
    default_database: str | None = None,
) -> dict[str, str] | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        port = parsed.port or default_port
    except ValueError:
        return None
    database = unquote(parsed.path.lstrip("/")) or default_database
    if not parsed.scheme or not host or database is None:
        return None
    return {
        "scheme": parsed.scheme.split("+", 1)[0].lower(),
        "host": host.lower(),
        "port": str(port),
        "database": database,
    }


def _oss_resource_identity(
    endpoint: str | None,
    bucket_name: str | None,
) -> dict[str, str] | None:
    if not endpoint or not bucket_name:
        return None
    raw_endpoint = endpoint.strip().rstrip("/")
    try:
        parsed = urlsplit(
            raw_endpoint if "://" in raw_endpoint else f"//{raw_endpoint}"
        )
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    normalized_endpoint = host.lower()
    if port is not None:
        normalized_endpoint = f"{normalized_endpoint}:{port}"
    return {
        "endpoint": normalized_endpoint,
        "bucket": bucket_name.strip().lower(),
    }


def _deployment_resource_fingerprints(
    settings: DeploymentSettings | None,
) -> dict[str, str | None]:
    if settings is None:
        return {
            "postgresql": None,
            "redis": None,
            "oss_bucket": None,
            "provider_account": None,
        }
    database_identity = _url_resource_identity(
        settings.database_url,
        default_port=5432,
    )
    database_resource_id = (settings.database_resource_id or "").strip()
    redis_resource_id = (settings.redis_resource_id or "").strip()
    provider_account_id = (settings.provider_account_id or "").strip()
    return {
        "postgresql": _sha256_fingerprint(
            {
                "resource_id": database_resource_id,
                "database": database_identity["database"],
            }
            if database_resource_id and database_identity
            else None
        ),
        "redis": _sha256_fingerprint(
            {"resource_id": redis_resource_id} if redis_resource_id else None
        ),
        "oss_bucket": _sha256_fingerprint(
            _oss_resource_identity(settings.oss_endpoint, settings.oss_bucket_name)
        ),
        "provider_account": _sha256_fingerprint(
            {"account_id": provider_account_id} if provider_account_id else None
        ),
    }


def _configuration_response(configuration: StoredConfiguration) -> dict[str, Any]:
    return {
        "id": configuration.id,
        "version_number": configuration.version_number,
        "status": configuration.status,
        "schema_version": configuration.schema_version,
        "reason": configuration.draft.reason,
        "platform": configuration.draft.platform.model_dump(mode="json"),
        "routes": [route.model_dump(mode="json") for route in configuration.draft.routes],
        "created_by_user_id": configuration.created_by_user_id,
        "created_at": configuration.created_at,
        "activated_at": configuration.activated_at,
        "superseded_at": configuration.superseded_at,
    }


def install_cloud_configuration_api(
    app: FastAPI,
    auth: AuthApplication,
    settings: DeploymentSettings | None = None,
) -> ConfigurationService:
    service = ConfigurationService(auth.database)
    observed_worker_concurrency = (
        settings.global_worker_concurrency if settings is not None else 8
    )
    router = APIRouter(prefix="/admin/configuration", tags=["平台配置管理"])

    def require_admin(request: Request) -> UserContext:
        token = request.cookies.get("lumenx_session")
        if not token:
            raise SessionAuthenticationError("AUTH_REQUIRED", "请先登录")
        csrf_token = (
            request.headers.get("x-csrf-token")
            if request.method in UNSAFE_METHODS
            else None
        )
        principal: SessionPrincipal = auth.sessions.resolve(token, csrf_token=csrf_token)
        if not principal.is_platform_admin:
            raise AdminAuthorizationError("仅平台管理员可以管理平台配置")
        return UserContext(
            user_id=str(principal.user_id),
            session_id=str(principal.session_id),
            is_platform_admin=True,
        )

    @router.get("/versions")
    def list_versions(identity: UserContext = Depends(require_admin)) -> list[dict[str, Any]]:
        return [
            _configuration_response(configuration)
            for configuration in service.list_versions(identity)
        ]

    @router.get("/active")
    def get_active(identity: UserContext = Depends(require_admin)) -> dict[str, Any]:
        return _configuration_response(service.get_active(identity))

    @router.get("/deployment-state")
    def get_deployment_state(
        _identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "worker_concurrency": observed_worker_concurrency,
            "authority": "environment_or_compose",
            "activation_required": True,
            "deployment_mode": settings.deployment_mode.value if settings else "unknown",
            "object_store_adapter": (
                settings.object_store_adapter.value if settings else "unknown"
            ),
            "provider_adapter": (
                settings.provider_adapter.value if settings else "unknown"
            ),
            "test_adapters_enabled": (
                settings.test_adapters_enabled if settings else False
            ),
            "oss_private": settings.oss_private if settings else False,
            "registration_emergency_disabled": (
                settings.registration_emergency_disabled if settings else True
            ),
            "new_ai_tasks_emergency_disabled": (
                settings.new_ai_tasks_emergency_disabled if settings else True
            ),
            "resource_fingerprints": _deployment_resource_fingerprints(settings),
        }

    @router.get("/versions/{version_id}")
    def get_version(
        version_id: str,
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return _configuration_response(service.get_version(identity, version_id))

    @router.post("/versions", status_code=201)
    def create_version(
        payload: ConfigurationDraft,
        request: Request,
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return _configuration_response(
            service.create_version(
                identity,
                payload,
                correlation_id=request.headers.get("x-correlation-id"),
            )
        )

    @router.post("/versions/{version_id}/validate")
    def validate_version(
        version_id: str,
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        configuration = service.validate_version(identity, version_id)
        return {
            "valid": True,
            "message": "配置校验通过，可以激活",
            "configuration": _configuration_response(configuration),
        }

    @router.post("/versions/{version_id}/activate")
    def activate_version(
        version_id: str,
        payload: ConfigurationReasonRequest,
        request: Request,
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return _configuration_response(
            service.activate_version(
                identity,
                version_id,
                reason=payload.reason,
                correlation_id=request.headers.get("x-correlation-id"),
            )
        )

    @router.post("/versions/{version_id}/disable")
    def disable_version(
        version_id: str,
        payload: ConfigurationReasonRequest,
        request: Request,
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return _configuration_response(
            service.disable_version(
                identity,
                version_id,
                reason=payload.reason,
                correlation_id=request.headers.get("x-correlation-id"),
            )
        )

    @router.post("/versions/{version_id}/rollback")
    def rollback_version(
        version_id: str,
        payload: ConfigurationReasonRequest,
        request: Request,
        identity: UserContext = Depends(require_admin),
    ) -> dict[str, Any]:
        return _configuration_response(
            service.rollback_to_new_version(
                identity,
                version_id,
                reason=payload.reason,
                correlation_id=request.headers.get("x-correlation-id"),
            )
        )

    app.include_router(router)
    app.state.configuration_service = service

    @app.exception_handler(ConfigurationNotFoundError)
    def handle_not_found(_request: Request, exc: ConfigurationNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "CONFIGURATION_NOT_FOUND", "message": str(exc)},
        )

    @app.exception_handler(ConfigurationConflictError)
    def handle_conflict(_request: Request, exc: ConfigurationConflictError):
        return JSONResponse(
            status_code=409,
            content={"code": "CONFIGURATION_CONFLICT", "message": str(exc)},
        )

    @app.exception_handler(ConfigurationValidationError)
    def handle_invalid(_request: Request, exc: ConfigurationValidationError):
        return JSONResponse(
            status_code=422,
            content={"code": "CONFIGURATION_INVALID", "message": str(exc)},
        )

    return service
