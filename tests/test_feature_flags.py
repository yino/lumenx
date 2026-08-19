from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.platform.ai_gateway_api import FeatureGatedAITaskSubmitter
from src.platform.auth.api import create_auth_router
from src.platform.configuration_schemas import PlatformFeatureFlags
from src.platform.configuration_service import ConfigurationService
from src.platform.contracts import AdminContext
from src.platform.feature_flags import CloudFeatureDisabledError, CloudFeatureGate
from src.platform.db_models import (
    AuditEventRecord,
    ConfigVersionRecord,
    ModelConfigRecord,
    PlatformConfigRecord,
)
from tests.test_configuration_service import _draft
from tests.test_content_repositories import RepositoryDatabase, _create_scope


@pytest.fixture
def feature_database():
    database = RepositoryDatabase()
    ConfigVersionRecord.__table__.create(database.engine)
    ModelConfigRecord.__table__.create(database.engine)
    PlatformConfigRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)
    yield database, AdminContext(admin_id="9001", session_id="9101", username="admin")
    database.engine.dispose()


def _activate(database, admin, *, registration: bool, ai_tasks: bool) -> None:
    service = ConfigurationService(database)
    draft = _draft().model_copy(
        update={
            "platform": _draft().platform.model_copy(
                update={
                    "feature_flags": PlatformFeatureFlags(
                        registration_enabled=registration,
                        new_ai_tasks_enabled=ai_tasks,
                    )
                }
            )
        }
    )
    stored = service.create_version(admin, draft)
    service.activate_version(admin, stored.id, reason="测试分阶段开放")


def test_feature_gate_is_fail_closed_and_emergency_flags_only_disable(
    feature_database,
) -> None:
    database, admin = feature_database
    gate = CloudFeatureGate(database)

    with pytest.raises(CloudFeatureDisabledError) as registration_error:
        gate.require_registration()
    with pytest.raises(CloudFeatureDisabledError) as ai_error:
        gate.require_new_ai_task()
    assert registration_error.value.code == "REGISTRATION_DISABLED"
    assert ai_error.value.code == "AI_NEW_TASKS_DISABLED"

    _activate(database, admin, registration=True, ai_tasks=True)
    assert gate.state().registration_enabled is True
    assert gate.state().new_ai_tasks_enabled is True

    emergency_gate = CloudFeatureGate(
        database,
        registration_emergency_disabled=True,
        new_ai_tasks_emergency_disabled=True,
    )
    assert emergency_gate.state().registration_enabled is False
    assert emergency_gate.state().new_ai_tasks_enabled is False


def test_registration_gate_blocks_before_creating_user(feature_database) -> None:
    database, _admin = feature_database
    application = SimpleNamespace(
        database=database,
        registration=Mock(),
        feature_gate=CloudFeatureGate(database),
        rate_limiter=Mock(),
        registration_rate=Mock(),
        authentication=Mock(),
        sessions=Mock(),
        user_administration=Mock(),
        support_reset=Mock(),
        login_rate=Mock(),
        reset_rate=Mock(),
    )
    app = FastAPI()
    app.include_router(create_auth_router(application))

    @app.exception_handler(CloudFeatureDisabledError)
    def handle_disabled(_request: Request, exc: CloudFeatureDisabledError):
        return JSONResponse(
            status_code=503,
            content={"code": exc.code, "message": str(exc)},
        )

    client = TestClient(app)
    response = client.post(
        "/auth/register",
        json={"phone": "13800138000", "password": "SecurePass123"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "REGISTRATION_DISABLED"
    application.registration.register.assert_not_called()


def test_ai_gate_blocks_only_new_submission() -> None:
    delegate = Mock()
    gate = Mock()
    gate.require_new_ai_task.side_effect = CloudFeatureDisabledError(
        "AI_NEW_TASKS_DISABLED",
        "AI 新任务已暂停，已有任务仍可查询和处理",
    )
    submitter = FeatureGatedAITaskSubmitter(delegate, gate)

    with pytest.raises(CloudFeatureDisabledError) as captured:
        submitter.submit(Mock(), {"capability": "image.t2i"})

    assert captured.value.code == "AI_NEW_TASKS_DISABLED"
    delegate.submit.assert_not_called()
