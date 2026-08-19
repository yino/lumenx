from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.platform.admin_inspection import (
    AdminInspectionNotFoundError,
    AdminInspectionService,
    AdminInspectionValidationError,
)
from src.platform.admin_inspection_api import install_cloud_admin_inspection_api
from src.platform.auth.admin import AdminAuthorizationError
from src.platform.auth.admin_identity import AdminSessionPrincipal
from src.platform.auth.sessions import SessionAuthenticationError
from src.platform.contracts import AdminContext
from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    AssetRecord,
    AuditEventRecord,
    AuthSessionRecord,
    ManualRechargeOrderRecord,
    ManualRechargeReconciliationReportRecord,
    MediaObjectRecord,
    ProjectRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    TicketWalletRecord,
    UsageEventRecord,
)
from src.platform.media_storage import CloudMediaStorage
from src.platform.settings import DeploymentSettings
from tests.test_content_api import FakeSessions
from tests.test_content_repositories import RepositoryDatabase, _create_scope


class InspectionDatabase(RepositoryDatabase):
    def __init__(self) -> None:
        super().__init__()
        for table in (
            AuthSessionRecord,
            TicketWalletRecord,
            AITaskRecord,
            AITaskAttemptRecord,
            TicketHoldRecord,
            TicketLedgerRecord,
            UsageEventRecord,
            ManualRechargeOrderRecord,
            ManualRechargeReconciliationReportRecord,
            AuditEventRecord,
        ):
            table.__table__.create(self.engine)


class PreviewObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.signed_calls: list[tuple[str, int]] = []

    def put(self, object_key: str, content: bytes, _content_type: str) -> None:
        self.objects[object_key] = content

    def signed_get_url(self, object_key: str, expires_seconds: int) -> str:
        self.signed_calls.append((object_key, expires_seconds))
        if object_key not in self.objects:
            raise RuntimeError("missing")
        return f"https://preview.example.test/{object_key.rsplit('/', 1)[-1]}?ttl={expires_seconds}"

    def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)


@pytest.fixture
def inspection_scope():
    database = InspectionDatabase()
    admin_scope = _create_scope(database)
    target_scope = _create_scope(database)
    other_scope = _create_scope(database)
    admin = AdminContext(admin_id="9001", session_id="9101", username="admin")
    target_user_id = int(target_scope.identity.user_id)
    target_workspace_id = int(target_scope.workspace_id)
    store = PreviewObjectStore()
    storage = CloudMediaStorage(database, store, namespace_prefix="lumenx")

    now = datetime.now(UTC)
    with database.session_factory.begin() as session:
        session.add(
            TicketWalletRecord(
                user_id=target_user_id,
                available_microtickets=4_000_000,
                held_microtickets=1_000_000,
                lifetime_granted_microtickets=2_000_000,
                lifetime_recharged_microtickets=3_000_000,
            )
        )
        project = ProjectRecord(
            user_id=target_user_id,
            workspace_id=target_workspace_id,
            title="隐私剧本",
            payload={"id": "project-domain-id", "original_text": "这是完整剧本正文"},
            schema_version=1,
            version=1,
        )
        session.add(project)
        session.flush()
        media = MediaObjectRecord(
            scope="user",
            user_id=target_user_id,
            workspace_id=target_workspace_id,
            project_id=project.id,
            object_key="lumenx/users/private/secret-cover.png",
            mime_type="image/png",
            size_bytes=16,
            checksum_sha256="a" * 64,
            lifecycle_state="active",
            provenance={"origin": "upload", "private_note": "不应返回"},
        )
        session.add(media)
        session.flush()
        task = AITaskRecord(
            user_id=target_user_id,
            workspace_id=target_workspace_id,
            project_id=project.id,
            capability="=image.formula",
            status="support_review",
            idempotency_key="inspection-task",
            request_fingerprint="f" * 64,
            request_payload={"prompt": "用户私密提示词", "provider_secret": "never"},
            config_snapshot={
                "display_name": "平台图像模型",
                "provider": "dashscope",
                "provider_model_id": "wanx-v1",
                "secret_ref": "env://NEVER",
            },
            tokens_per_ticket=1000,
            quoted_microtickets=2_000_000,
            provider_billable=True,
            support_review_reason="供应商已计费但结果待核查",
            result={"media_id": str(media.id), "raw_provider": "不返回"},
            updated_at=now,
        )
        session.add(task)
        session.flush()
        session.add(
            AITaskAttemptRecord(
                user_id=target_user_id,
                workspace_id=target_workspace_id,
                task_id=task.id,
                retry_key="inspection-attempt",
                attempt_number=1,
                status="ambiguous",
                config_snapshot={"secret_ref": "env://NEVER"},
                provider="dashscope",
                provider_model_id="wanx-v1",
                diagnostic={"raw_error": "不应返回"},
                billable_acknowledged_at=now,
            )
        )
        session.add(
            TicketHoldRecord(
                user_id=target_user_id,
                workspace_id=target_workspace_id,
                task_id=task.id,
                quoted_microtickets=1_000_000,
                remaining_microtickets=1_000_000,
                status="held",
                created_at=now - timedelta(hours=1),
            )
        )
        session.add(
            UsageEventRecord(
                user_id=target_user_id,
                workspace_id=target_workspace_id,
                project_id=project.id,
                task_id=task.id,
                capability="image.generation",
                outcome="billable_failure",
                raw_provider_usage={"cost": "private"},
                metering_formula={"type": "image"},
                metering_tokens=1000,
                tokens_per_ticket=1000,
                charged_microtickets=1_000_000,
                created_at=now,
            )
        )
        session.add(
            ManualRechargeOrderRecord(
                order_number="MR202608160001",
                user_id=target_user_id,
                cash_amount_fen=1000,
                ticket_amount_microtickets=3_000_000,
                currency="CNY",
                status="completed",
                exchange_snapshot={"tokens_per_ticket": 1000},
                offline_reference="PRIVATE-OFFLINE-REF",
                create_reason="财务线下登记",
                create_idempotency_key="inspection-order",
                create_request_fingerprint="o" * 64,
                version=2,
                created_by_admin_id=int(admin.admin_id),
                completed_by_admin_id=int(admin.admin_id),
                completed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ManualRechargeReconciliationReportRecord(
                order_id=1,
                user_id=target_user_id,
                actor_admin_id=int(admin.admin_id),
                status="mismatch",
                severity="high",
                details={"private": "internal details"},
                correlation_id="inspection-reconcile",
                created_at=now,
            )
        )
        session.add(
            AuthSessionRecord(
                user_id=target_user_id,
                token_hash="t" * 64,
                csrf_token_hash="c" * 64,
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(days=1),
                network_fingerprint="private-network",
                user_agent="Test Browser",
            )
        )
    store.objects["lumenx/users/private/secret-cover.png"] = b"private-image"
    service = AdminInspectionService(database, storage)
    yield database, admin, target_scope, other_scope, storage, service
    database.engine.dispose()


def test_admin_inspection_is_scoped_masked_and_read_only(inspection_scope) -> None:
    database, admin, target, other, _storage, service = inspection_scope
    user_id = int(target.identity.user_id)
    workspace_id = int(target.workspace_id)

    overview = service.user_overview(admin, user_id)
    assert overview["wallet"]["available_tickets"] == "4"
    assert overview["counts"]["projects"] == 1
    assert overview["exceptions"]["total"] == 2
    assert "phone_canonical" not in str(overview)

    projects = service.list_resources(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        resource="projects",
        offset=0,
        limit=30,
    )
    media = service.list_resources(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        resource="media",
        offset=0,
        limit=30,
    )
    sessions = service.list_resources(
        admin,
        user_id=user_id,
        workspace_id=None,
        resource="sessions",
        offset=0,
        limit=30,
    )
    assert projects["items"][0]["title"] == "隐私剧本"
    assert "original_text" not in str(projects)
    assert "object_key" not in str(media)
    assert "token_hash" not in str(sessions)
    assert "network_fingerprint" not in str(sessions)

    with pytest.raises(AdminInspectionNotFoundError):
        service.list_resources(
            admin,
            user_id=user_id,
            workspace_id=int(other.workspace_id),
            resource="assets",
            offset=0,
            limit=30,
        )

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(MediaObjectRecord)) == 1


def test_sensitive_views_require_chinese_purpose_audit_and_hide_raw_diagnostics(
    inspection_scope,
) -> None:
    database, admin, target, _other, _storage, service = inspection_scope
    user_id = int(target.identity.user_id)
    workspace_id = int(target.workspace_id)
    with database.session_factory() as session:
        project_id = session.scalar(select(ProjectRecord.id))
        task_id = session.scalar(select(AITaskRecord.id))
        media_id = session.scalar(select(MediaObjectRecord.id))

    with pytest.raises(AdminInspectionValidationError) as missing_purpose:
        service.sensitive_script(
            admin,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            purpose="support",
            correlation_id="script-denied",
        )
    assert missing_purpose.value.code == "SENSITIVE_PURPOSE_REQUIRED"

    script = service.sensitive_script(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        project_id=project_id,
        purpose="客服排查用户反馈的剧本问题",
        correlation_id="script-view",
    )
    prompt = service.sensitive_task_prompt(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        task_id=task_id,
        purpose="客服排查任务生成结果异常",
        correlation_id="prompt-view",
    )
    preview = service.media_preview(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        media_id=media_id,
        purpose="客服核对用户反馈的图片结果",
        correlation_id="media-view",
    )
    detail = service.task_detail(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    assert script["text"] == "这是完整剧本正文"
    assert prompt["request_text"] == {"prompt": "用户私密提示词"}
    assert "provider_secret" not in str(prompt)
    assert "raw_error" not in str(prompt)
    assert detail["result_media_ids"] == [str(media_id)]
    assert "request_payload" not in str(detail)
    assert "diagnostic" not in str(detail)
    assert preview["disposition"] == "inline"
    assert "secret-cover.png" in preview["url"]
    assert "object_key" not in preview
    with database.session_factory() as session:
        actions = set(session.scalars(select(AuditEventRecord.action)))
    assert {
        "admin.sensitive.script.view",
        "admin.sensitive.task_prompt.view",
        "admin.sensitive.media.preview",
    } <= actions


def test_dashboard_exports_and_api_authorization_are_bounded(inspection_scope) -> None:
    database, admin, target, _other, storage, service = inspection_scope
    user_id = int(target.identity.user_id)
    now = datetime.now(UTC)
    dashboard = service.dashboard(admin, start_at=now - timedelta(days=1), end_at=now + timedelta(seconds=1))
    assert dashboard["users"]["total"] == 3
    assert dashboard["orders"]["paid_count"] == 1
    assert dashboard["tasks"]["support_review"] == 1
    assert dashboard["exceptions"]["total"] >= 2
    stale_holds = [
        item for item in dashboard["exceptions"]["items"] if item["kind"] == "stale_hold"
    ]
    assert len(stale_holds) == 1
    assert stale_holds[0]["resource_type"] == "ticket_hold"
    with pytest.raises(AdminInspectionValidationError):
        service.dashboard(admin, start_at=now - timedelta(days=100), end_at=now)

    csv_content = service.export_csv(
        admin,
        resource="tasks",
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(seconds=1),
        user_id=user_id,
        status=None,
        purpose="运营导出任务列表进行问题排查",
        correlation_id="export-tasks",
    )
    assert csv_content.startswith("\ufeff任务ID")
    assert "'=image.formula" in csv_content
    assert "用户私密提示词" not in csv_content
    with database.session_factory() as session:
        assert session.scalar(
            select(func.count(AuditEventRecord.id)).where(
                AuditEventRecord.action == "admin.export.tasks"
            )
        ) == 1

    sessions = FakeSessions(
        AdminSessionPrincipal(
            admin_id=int(admin.admin_id),
            session_id=int(admin.session_id or "1"),
            username="admin",
            must_change_password=False,
        )
    )
    app = FastAPI()
    install_cloud_admin_inspection_api(
        app,
        SimpleNamespace(database=database, admin_sessions=sessions),
        DeploymentSettings(_env_file=None),
        storage,
    )

    @app.exception_handler(AdminAuthorizationError)
    def denied(_request: Request, exc: AdminAuthorizationError):
        return JSONResponse(status_code=403, content={"code": "ADMIN_REQUIRED", "message": str(exc)})

    @app.exception_handler(SessionAuthenticationError)
    def unauthenticated(_request: Request, exc: SessionAuthenticationError):
        return JSONResponse(status_code=401, content={"code": exc.code, "message": str(exc)})

    client = TestClient(app)
    client.cookies.set("lumenx_admin_session", "session-token")
    overview = client.get(f"/admin/users/{user_id}/overview")
    assert overview.status_code == 200
    client.cookies.delete("lumenx_admin_session")
    client.cookies.set("lumenx_session", "normal-user-session")
    denied_response = client.get(f"/admin/users/{user_id}/overview")
    assert denied_response.status_code == 401
    assert "隐私剧本" not in denied_response.text
    client.close()


def test_sensitive_audit_failure_blocks_script_prompt_and_media_response(
    inspection_scope,
    monkeypatch,
) -> None:
    database, admin, target, _other, storage, service = inspection_scope
    user_id = int(target.identity.user_id)
    workspace_id = int(target.workspace_id)
    with database.session_factory() as session:
        project_id = session.scalar(select(ProjectRecord.id))
        task_id = session.scalar(select(AITaskRecord.id))
        media_id = session.scalar(select(MediaObjectRecord.id))

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(service, "_audit_sensitive", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.sensitive_script(
            admin,
            user_id=user_id,
            workspace_id=workspace_id,
            project_id=project_id,
            purpose="客服排查用户反馈的剧本问题",
            correlation_id="audit-fail-script",
        )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.sensitive_task_prompt(
            admin,
            user_id=user_id,
            workspace_id=workspace_id,
            task_id=task_id,
            purpose="客服排查任务生成结果异常",
            correlation_id="audit-fail-prompt",
        )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.media_preview(
            admin,
            user_id=user_id,
            workspace_id=workspace_id,
            media_id=media_id,
            purpose="客服核对用户反馈的图片结果",
            correlation_id="audit-fail-media",
        )
    assert storage.object_store.signed_calls == []
    with database.session_factory() as session:
        assert session.scalar(
            select(func.count(AuditEventRecord.id)).where(
                AuditEventRecord.action.like("admin.sensitive.%")
            )
        ) == 0


def test_export_rejects_invalid_purpose_caps_and_csv_formula_prefixes(inspection_scope) -> None:
    _database, admin, target, _other, _storage, service = inspection_scope
    now = datetime.now(UTC)
    with pytest.raises(AdminInspectionValidationError) as purpose:
        service.export_csv(
            admin,
            resource="users",
            start_at=now - timedelta(days=1),
            end_at=now,
            user_id=None,
            status=None,
            purpose="support export",
            correlation_id="invalid-purpose",
        )
    assert purpose.value.code == "EXPORT_FILTER_INVALID"
    with pytest.raises(AdminInspectionValidationError, match="上限无效"):
        service.export_csv(
            admin,
            resource="users",
            start_at=now - timedelta(days=1),
            end_at=now,
            user_id=int(target.identity.user_id),
            status=None,
            purpose="运营导出用户列表",
            correlation_id="invalid-cap",
            row_cap=0,
        )
    assert service._csv_safe("=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert service._csv_safe("+cmd") == "'+cmd"
    assert service._csv_safe("@formula") == "'@formula"


def test_large_asset_and_task_directories_remain_bounded_and_stable(
    inspection_scope,
) -> None:
    database, admin, target, _other, _storage, service = inspection_scope
    user_id = int(target.identity.user_id)
    workspace_id = int(target.workspace_id)
    with database.session_factory.begin() as session:
        for sequence in range(125):
            session.add(
                AssetRecord(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    scope="workspace",
                    asset_type="scene",
                    name=f"分页场景 {sequence:03d}",
                    payload={},
                    provenance={},
                    schema_version=1,
                    version=1,
                )
            )
            session.add(
                AITaskRecord(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    capability="image.t2i",
                    status="succeeded",
                    idempotency_key=f"page-task-{sequence}",
                    request_fingerprint=f"{sequence:064d}"[-64:],
                    request_payload={},
                    config_snapshot={},
                    tokens_per_ticket=1000,
                    quoted_microtickets=1000,
                    provider_billable=False,
                )
            )

    asset_first = service.list_resources(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        resource="assets",
        offset=0,
        limit=100,
    )
    asset_second = service.list_resources(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        resource="assets",
        offset=100,
        limit=100,
    )
    task_first = service.list_resources(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        resource="tasks",
        offset=0,
        limit=100,
    )
    task_second = service.list_resources(
        admin,
        user_id=user_id,
        workspace_id=workspace_id,
        resource="tasks",
        offset=100,
        limit=100,
    )

    assert asset_first["total"] == 125
    assert len(asset_first["items"]) == 100
    assert len(asset_second["items"]) == 25
    assert task_first["total"] == 126
    assert len(task_first["items"]) == 100
    assert len(task_second["items"]) == 26
    for first, second in (
        (asset_first, asset_second),
        (task_first, task_second),
    ):
        assert {item["id"] for item in first["items"]}.isdisjoint(
            {item["id"] for item in second["items"]}
        )
