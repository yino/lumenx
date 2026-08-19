from __future__ import annotations

import json
from pathlib import Path

from src.platform.configuration_schemas import AICapability, ConfigurationDraft
from src.platform.configuration_service import ConfigurationService
from src.platform.contracts import AdminContext
from src.platform.db_models import (
    AdminUserRecord,
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
)
from tests.test_content_repositories import RepositoryDatabase, _create_scope


def test_local_ai_bootstrap_contract_is_local_only() -> None:
    script = Path("scripts/set_local_ai_mode.py").read_text(encoding="utf-8")
    assert "LUMENX_LOCAL_DOCKER" in script
    assert "LUMENX_LOCAL_AI_BOOTSTRAP_ENABLED" in script
    assert "DASHSCOPE_API_KEY" in script


def test_local_ai_routes_are_valid_and_versioned() -> None:
    database = RepositoryDatabase()
    ConfigVersionRecord.__table__.create(database.engine)
    ModelConfigRecord.__table__.create(database.engine)
    PlatformConfigRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    AdminUserRecord.__table__.create(database.engine)
    _create_scope(database)
    admin = AdminContext(admin_id="9001", session_id="local-test", username="admin")
    try:
        with database.session_factory.begin() as session:
            session.add(
                AdminUserRecord(
                    id=9001,
                    username="admin",
                    password_hash="test-password-hash",
                    status="active",
                )
            )
        from scripts.set_local_ai_mode import (
            CATALOG_ROUTE_MODELS,
            TEXT_CAPABILITIES,
            _catalog_route,
            _default_route,
            _speech_route,
        )

        service = ConfigurationService(database)
        active = service.create_version(
            admin,
            ConfigurationDraft.model_validate(
                {
                    "reason": "本地安全策略",
                    "platform": {
                        "tokens_per_ticket": 1000,
                        "session_idle_seconds": 3600,
                        "session_absolute_seconds": 86400,
                        "max_sessions_per_user": 5,
                        "max_ai_concurrency_per_user": 2,
                        "exposed_capabilities": [],
                    },
                    "routes": [],
                }
            ),
        )
        service.activate_version(admin, active.id, reason="启用本地安全策略")
        current = service.get_active(admin)
        catalog = json.loads(
            Path("config/model_catalog/generated/model_catalog.json").read_text(
                encoding="utf-8"
            )
        )
        routes = [
            *(
                _default_route(capability, "qwen3.6-plus")
                for capability in TEXT_CAPABILITIES
            ),
            _speech_route(),
            *(
                _catalog_route(capability, model_id, catalog)
                for capability, model_id in CATALOG_ROUTE_MODELS.items()
            ),
        ]
        exposed_capabilities = [route.capability for route in routes]
        draft = current.draft.model_copy(
            update={
                "platform": current.draft.platform.model_copy(
                    update={
                        "exposed_capabilities": exposed_capabilities,
                        "feature_flags": current.draft.platform.feature_flags.model_copy(
                            update={"new_ai_tasks_enabled": True}
                        ),
                    }
                ),
                "routes": routes,
                "reason": "测试本地 AI 配置",
            }
        )
        created = service.create_version(admin, draft)
        activated = service.activate_version(admin, created.id, reason="测试本地 AI 配置")
        assert activated.draft.platform.feature_flags.new_ai_tasks_enabled is True
        assert {route.capability for route in activated.draft.routes} == {
            AICapability.SCRIPT_ANALYSIS,
            AICapability.PROMPT_POLISH,
            AICapability.IMAGE_T2I,
            AICapability.IMAGE_I2I,
            AICapability.VIDEO_I2V,
            AICapability.VIDEO_R2V,
            AICapability.SPEECH_TTS,
        }
        assert all(route.enabled and route.is_primary for route in activated.draft.routes)
        assert all(
            not route.metering_formula.review_required
            for route in activated.draft.routes
            if route.capability in CATALOG_ROUTE_MODELS
        )
        speech = next(
            route
            for route in activated.draft.routes
            if route.capability is AICapability.SPEECH_TTS
        )
        assert speech.provider_model_id == "cosyvoice-v2"
        assert speech.metering_formula.kind == "speech"
        video_routes = {
            route.capability: route
            for route in activated.draft.routes
            if route.capability in {AICapability.VIDEO_I2V, AICapability.VIDEO_R2V}
        }
        assert video_routes[AICapability.VIDEO_I2V].provider_model_id == (
            "seedance-2.0-i2v"
        )
        assert video_routes[AICapability.VIDEO_R2V].provider_model_id == (
            "seedance-2.0-r2v"
        )
        assert {
            (route.provider, route.secret_ref) for route in video_routes.values()
        } == {("ark", "ARK_API_KEY")}
    finally:
        database.engine.dispose()
