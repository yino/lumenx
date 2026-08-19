from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.platform.auth.admin import AdminAuthorizationError
from src.platform.auth.admin_identity import AdminSessionPrincipal
from src.platform.auth.sessions import SessionAuthenticationError
from src.platform.db_models import (
    AITaskRecord,
    AuditEventRecord,
    ImportBatchItemRecord,
    ImportBatchRecord,
    ImportedPlaygroundHistoryRecord,
)
from src.platform.import_api import install_cloud_import_api
from src.platform.media_storage import CloudMediaStorage
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_media_storage import FakePrivateObjectStore


def _client(tmp_path):
    database = RepositoryDatabase()
    ImportBatchRecord.__table__.create(database.engine)
    ImportBatchItemRecord.__table__.create(database.engine)
    ImportedPlaygroundHistoryRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    AITaskRecord.__table__.create(database.engine)
    context = _create_scope(database)
    sessions = Mock()
    sessions.resolve.return_value = AdminSessionPrincipal(
        admin_id=9001,
        session_id=9101,
        username="admin",
        must_change_password=False,
    )
    app = FastAPI()
    install_cloud_import_api(
        app,
        SimpleNamespace(database=database, admin_sessions=sessions),
        CloudMediaStorage(database, FakePrivateObjectStore()),
        allowed_root=tmp_path,
    )

    @app.exception_handler(AdminAuthorizationError)
    def handle_admin_error(_request: Request, exc: AdminAuthorizationError):
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
    return database, context, sessions, client


def test_import_api_maps_target_validation_to_stable_error(tmp_path) -> None:
    database, context, _sessions, client = _client(tmp_path)
    response = client.post(
        "/admin/imports/dry-run",
        json={
            "target_user_id": context.identity.user_id,
            "target_workspace_id": "999999",
            "source_directory": "missing",
            "include_playground": True,
            "missing_media_policy": "reject",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "IMPORT_VALIDATION_FAILED",
        "message": "目标用户与工作区归属不匹配或不可用",
    }
    database.engine.dispose()


def test_import_api_denies_non_admin_before_reading_source(tmp_path) -> None:
    database, context, sessions, client = _client(tmp_path)
    client.cookies.delete("lumenx_admin_session")
    client.cookies.set("lumenx_session", "normal-user-session")
    response = client.post(
        "/admin/imports/dry-run",
        json={
            "target_user_id": context.identity.user_id,
            "target_workspace_id": context.workspace_id,
            "source_directory": "missing",
        },
    )
    assert response.status_code == 401
    assert response.json()["code"] == "ADMIN_AUTH_REQUIRED"
    database.engine.dispose()
