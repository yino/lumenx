from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.platform.administration_api import install_cloud_platform_administration_api
from src.platform.auth.admin import AdminAuthorizationError, UserAdministrationService
from src.platform.auth.security import PasswordService
from src.platform.auth.sessions import SessionPrincipal
from src.platform.db_models import (
    AITaskRecord,
    AuditEventRecord,
    ImportBatchRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
    UserRecord,
    WorkspaceRecord,
)
from tests.test_content_repositories import RepositoryDatabase, _create_scope


@pytest.fixture
def administration_client():
    database = RepositoryDatabase()
    TicketLedgerRecord.__table__.create(database.engine)
    TicketWalletRecord.__table__.create(database.engine)
    AITaskRecord.__table__.create(database.engine)
    UsageEventRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    ImportBatchRecord.__table__.create(database.engine)
    context = _create_scope(database)
    user_id = int(context.identity.user_id)
    workspace_id = int(context.workspace_id)
    with database.session_factory.begin() as session:
        session.add(
            TicketWalletRecord(
                user_id=user_id,
                available_microtickets=2_500_000,
                held_microtickets=500_000,
                lifetime_granted_microtickets=3_000_000,
            )
        )
        task = AITaskRecord(
            user_id=user_id,
            workspace_id=workspace_id,
            capability="image_generation",
            status="support_review",
            idempotency_key="admin-view-task",
            request_fingerprint="f" * 64,
            request_payload={"prompt": "不应出现在管理响应中"},
            config_snapshot={
                "display_name": "平台图像模型",
                "provider": "dashscope",
                "provider_model_id": "wan-image-v1",
                "secret_ref": "env://NEVER_EXPOSE",
            },
            tokens_per_ticket=1000,
            quoted_microtickets=1_250_000,
            provider_billable=True,
            support_review_reason="供应商已计费，媒体上传失败",
        )
        session.add(task)
        session.flush()
        session.add(
            UsageEventRecord(
                user_id=user_id,
                workspace_id=workspace_id,
                task_id=task.id,
                capability="image_generation",
                outcome="billable_failure",
                raw_provider_usage={"provider_cost": "private"},
                metering_formula={"type": "image"},
                metering_tokens=1000,
                tokens_per_ticket=1000,
                charged_microtickets=1_000_000,
            )
        )
        session.add(
            AuditEventRecord(
                actor_user_id=user_id,
                target_user_id=user_id,
                action="ticket.grant",
                target_type="ticket_wallet",
                target_id=str(user_id),
                reason="运营赠送",
                before_summary={"internal": "不返回"},
                after_summary={"internal": "不返回"},
                correlation_id="corr-admin-view",
            )
        )
        session.add(
            ImportBatchRecord(
                actor_admin_user_id=user_id,
                target_user_id=user_id,
                target_workspace_id=workspace_id,
                source_fingerprint="a" * 64,
                status="dry_run",
                options={"source_path": "/private/local/path"},
                dry_run_report={"planned": 2},
            )
        )

    sessions = Mock()
    sessions.resolve.return_value = SessionPrincipal(
        user_id=user_id,
        session_id=int(context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
        is_platform_admin=True,
    )
    app = FastAPI()
    runtime_policy = Mock()
    runtime_policy.resolve.return_value = SimpleNamespace(
        registration_initial_grant_microtickets=1_500_000,
        config_version_id="12",
    )
    install_cloud_platform_administration_api(
        app,
        SimpleNamespace(
            database=database,
            sessions=sessions,
            user_administration=UserAdministrationService(database, "s" * 32),
            runtime_policy=runtime_policy,
        ),
    )

    @app.exception_handler(AdminAuthorizationError)
    def handle_admin_denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(
            status_code=403,
            content={"code": "ADMIN_REQUIRED", "message": str(exc)},
        )

    client = TestClient(app)
    client.cookies.set("lumenx_session", "admin-session")
    yield client, sessions, context
    database.engine.dispose()


def test_platform_admin_can_inspect_safe_paginated_views(administration_client) -> None:
    client, _sessions, context = administration_client

    users = client.get("/admin/users", params={"query": "+86"})
    tasks = client.get("/admin/tasks", params={"status": "support_review"})
    usage = client.get("/admin/usage", params={"outcome": "billable_failure"})
    audit = client.get("/admin/audit-events", params={"action": "ticket"})
    batches = client.get("/admin/import-batches", params={"status": "dry_run"})

    assert users.status_code == 200
    assert users.json()["items"][0]["available_tickets"] == "2.5"
    assert tasks.status_code == 200
    assert tasks.json()["items"][0]["status_zh"] == "计费待复核"
    assert tasks.json()["items"][0]["actual_model"] == {
        "display_name": "平台图像模型",
        "model_id": "wan-image-v1",
        "provider": "dashscope",
    }
    assert "request_payload" not in tasks.text
    assert "secret_ref" not in tasks.text
    assert usage.status_code == 200
    assert usage.json()["total_metering_tokens"] == "1000"
    assert usage.json()["total_charged_tickets"] == "1"
    assert "provider_cost" not in usage.text
    assert audit.status_code == 200
    assert audit.json()["items"][0]["correlation_id"] == "corr-admin-view"
    assert "before_summary" not in audit.text
    assert batches.status_code == 200
    assert batches.json()["items"][0]["target_workspace_id"] == context.workspace_id
    assert batches.json()["items"][0]["has_dry_run_report"] is True
    assert "/private/local/path" not in batches.text


def test_platform_admin_can_create_user_with_workspace_wallet_and_audit(
    administration_client,
) -> None:
    client, sessions, context = administration_client

    response = client.post(
        "/admin/users",
        headers={"x-csrf-token": "csrf-admin-create"},
        json={
            "phone": "13800138088",
            "password": "secure-pass-2026",
            "reason": "运营后台开户",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["phone"] == "+8613800138088"
    assert payload["status"] == "active"
    sessions.resolve.assert_called_with(
        "admin-session",
        csrf_token="csrf-admin-create",
    )
    with client.app.state.platform_administration_query_service.database.session_factory() as session:
        user = session.get(UserRecord, int(payload["id"]))
        workspace = session.get(WorkspaceRecord, int(payload["workspace_id"]))
        assert user is not None
        wallet = session.scalar(
            select(TicketWalletRecord).where(TicketWalletRecord.user_id == user.id)
        )
        audit = session.scalar(
            select(AuditEventRecord).where(
                AuditEventRecord.target_user_id == user.id,
                AuditEventRecord.action == "user.create",
            )
        )
        assert PasswordService().verify(user.password_hash, "secure-pass-2026") is True
        assert workspace is not None and workspace.user_id == user.id
        assert wallet is not None and wallet.available_microtickets == 1_500_000
        assert audit is not None and audit.reason == "运营后台开户"
        assert audit.actor_user_id == int(context.identity.user_id)


def test_platform_administration_denies_normal_user(administration_client) -> None:
    client, sessions, _context = administration_client
    sessions.resolve.return_value = replace(
        sessions.resolve.return_value,
        is_platform_admin=False,
    )

    for path in (
        "/admin/users",
        "/admin/tasks",
        "/admin/usage",
        "/admin/audit-events",
        "/admin/import-batches",
    ):
        response = client.get(path)
        assert response.status_code == 403
        assert response.json() == {
            "code": "ADMIN_REQUIRED",
            "message": "仅平台管理员可以查看平台管理数据",
        }

    denied_create = client.post(
        "/admin/users",
        headers={"x-csrf-token": "csrf-denied"},
        json={
            "phone": "13800138089",
            "password": "secure-pass-2026",
            "reason": "越权尝试",
        },
    )
    assert denied_create.status_code == 403
