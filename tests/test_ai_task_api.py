from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.platform.ai_task_api import install_cloud_ai_task_api
from src.platform.ai_task_state import AITaskStateService
from src.platform.auth.sessions import SessionPrincipal
from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    AuditEventRecord,
    TicketHoldRecord,
    UsageEventRecord,
)
from src.platform.ticket_reservation import TicketReservationService
from tests.test_content_api import FakeSessions, _headers
from tests.test_content_repositories import _create_scope
from tests.test_ticket_reservation import _reservation_database
from tests.test_ticket_settlement import LLM_FORMULA


def _reserve_task(database, context, *, key: str):
    reservation = TicketReservationService(database).reserve_task(
        context,
        capability="script.analysis",
        idempotency_key=key,
        request_payload={"content": "测试剧本", "parameters": {}},
        config_snapshot={
            "config_version_id": str(uuid.uuid4()),
            "capability": "script.analysis",
            "provider": "dashscope",
            "provider_model_id": "qwen-plus",
            "display_name": "剧本分析模型",
            "secret_ref": "env://DASHSCOPE_API_KEY",
            "parameters": {},
            "metering_formula": LLM_FORMULA,
        },
        maximum_metering_tokens=1000,
        tokens_per_ticket=1000,
    )
    state = AITaskStateService(database)
    attempt = state.create_initial_attempt(
        context,
        task_id=reservation.task_id,
        config_snapshot={
            "provider": "dashscope",
            "provider_model_id": "qwen-plus",
            "display_name": "剧本分析模型",
            "secret_ref": "env://DASHSCOPE_API_KEY",
            "metering_formula": LLM_FORMULA,
        },
        provider="dashscope",
        provider_model_id="qwen-plus",
    )
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"reserved"},
        target_status="queued",
    )
    return reservation, attempt


@pytest.fixture
def ai_task_api():
    database, context = _reservation_database(initial_balance=5_000_000)
    AITaskAttemptRecord.__table__.create(database.engine)
    UsageEventRecord.__table__.create(database.engine)
    AuditEventRecord.__table__.create(database.engine)
    reservation, attempt = _reserve_task(database, context, key="ai-task-api-1")
    principal = SessionPrincipal(
        user_id=int(context.identity.user_id),
        session_id=int(context.identity.session_id),
        phone_canonical="+8613800138000",
        phone_verified=False,
    )
    sessions = FakeSessions(principal)
    app = FastAPI()
    install_cloud_ai_task_api(
        app,
        SimpleNamespace(database=database, sessions=sessions),
    )
    client = TestClient(app)
    client.cookies.set("lumenx_session", "session-token")
    yield database, context, sessions, client, reservation, attempt
    client.close()
    database.engine.dispose()


def test_task_list_detail_and_status_are_scoped_safe_and_paginated(ai_task_api) -> None:
    database, context, sessions, client, reservation, _attempt = ai_task_api
    _reserve_task(database, context, key="ai-task-api-2")

    missing_workspace = client.get("/ai/tasks")
    page = client.get(
        "/ai/tasks",
        params={"status": "queued", "offset": 0, "limit": 1},
        headers=_headers(context.workspace_id),
    )
    detail = client.get(
        f"/ai/tasks/{reservation.task_id}",
        headers=_headers(context.workspace_id),
    )
    status = client.get(
        f"/ai/tasks/{reservation.task_id}/status",
        headers=_headers(context.workspace_id),
    )

    assert missing_workspace.status_code == 400
    assert missing_workspace.json()["code"] == "WORKSPACE_CONTEXT_INVALID"
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["limit"] == 1
    assert len(page.json()["items"]) == 1
    assert detail.status_code == 200
    assert detail.json()["actual_model"] == {
        "display_name": "剧本分析模型",
        "model_id": "qwen-plus",
    }
    assert detail.json()["quoted_microtickets"] == "1000000"
    assert detail.json()["quoted_tickets"] == "1"
    assert detail.json()["support_review"] is False
    assert detail.json()["billing"]["open_hold_ids"] == [reservation.hold_id]
    assert detail.json()["attempts"][0]["model_id"] == "qwen-plus"
    assert "secret_ref" not in detail.text
    assert "provider_request_id" not in detail.text
    assert "raw_usage" not in detail.text
    assert "diagnostic" not in detail.text
    assert status.status_code == 200
    assert status.json()["status_zh"] == "排队中"
    assert sessions.calls[-1] == ("session-token", None)


def test_text_task_status_exposes_only_bounded_safe_result_content(ai_task_api) -> None:
    database, context, _sessions, client, reservation, _attempt = ai_task_api
    state = AITaskStateService(database)
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"queued"},
        target_status="running",
    )
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"running"},
        target_status="provider_succeeded",
        result={"content": "供应商原始文本", "provider_secret": "不得返回"},
    )
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"provider_succeeded"},
        target_status="succeeded",
        result={"content": "下一集从雨夜追逐开始。", "internal": "不得返回"},
    )

    status = client.get(
        f"/ai/tasks/{reservation.task_id}/status",
        headers=_headers(context.workspace_id),
    )
    detail = client.get(
        f"/ai/tasks/{reservation.task_id}",
        headers=_headers(context.workspace_id),
    )
    page = client.get(
        "/ai/tasks",
        headers=_headers(context.workspace_id),
    )

    assert status.status_code == 200
    assert status.json()["result_content"] == "下一集从雨夜追逐开始。"
    assert detail.json()["result_content"] == "下一集从雨夜追逐开始。"
    assert "internal" not in status.text
    assert "provider_secret" not in status.text
    assert "result_content" not in page.json()["items"][0]


def test_task_detail_hides_foreign_user_and_workspace(ai_task_api) -> None:
    database, context, sessions, client, reservation, _attempt = ai_task_api
    other_context = _create_scope(database)

    wrong_workspace = client.get(
        f"/ai/tasks/{reservation.task_id}",
        headers=_headers(other_context.workspace_id),
    )
    sessions.principal = SessionPrincipal(
        user_id=int(other_context.identity.user_id),
        session_id=int(other_context.identity.session_id),
        phone_canonical="+8613900139000",
        phone_verified=False,
    )
    wrong_user = client.get(
        f"/ai/tasks/{reservation.task_id}",
        headers=_headers(context.workspace_id),
    )

    assert wrong_workspace.status_code == 404
    assert wrong_workspace.json() == {
        "code": "AI_TASK_NOT_FOUND",
        "message": "AI 任务不存在",
    }
    assert wrong_user.status_code == 404
    assert wrong_user.json() == wrong_workspace.json()


def test_queued_cancellation_releases_hold_and_is_idempotent(ai_task_api) -> None:
    database, context, sessions, client, reservation, _attempt = ai_task_api
    response = client.post(
        f"/ai/tasks/{reservation.task_id}/cancel",
        headers=_headers(context.workspace_id),
    )
    repeated = client.post(
        f"/ai/tasks/{reservation.task_id}/cancel",
        headers=_headers(context.workspace_id),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["cancellation_outcome"] == "cancelled"
    assert response.json()["released_microtickets"] == "1000000"
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "cancelled"
    assert sessions.calls[-1] == ("session-token", "csrf-token")
    with database.session_factory() as session:
        task = session.get(AITaskRecord, int(reservation.task_id))
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        assert task is not None and task.cancellation_requested_at is not None
        assert hold is not None and hold.status == "released"
        assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 1
        audit_events = list(
            session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.action == "ai_task.cancel"
                )
            )
        )
        assert len(audit_events) == 2
        assert audit_events[0].workspace_id == int(context.workspace_id)


def test_running_cancellation_keeps_hold_until_nonbilling_is_confirmed(
    ai_task_api,
) -> None:
    database, context, _sessions, client, reservation, attempt = ai_task_api
    state = AITaskStateService(database)
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"queued"},
        target_status="running",
    )
    state.transition_attempt(
        context,
        task_id=reservation.task_id,
        attempt_id=attempt.id,
        expected_statuses={"pending"},
        target_status="running",
    )

    response = client.post(
        f"/ai/tasks/{reservation.task_id}/cancel",
        headers=_headers(context.workspace_id),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["cancellation_outcome"] == "requested"
    assert response.json()["released_microtickets"] == "0"
    assert response.json()["cancellation_requested"] is True
    assert response.json()["message"] == "已提交取消请求，等待供应商确认计费状态"
    with database.session_factory() as session:
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        assert hold is not None and hold.status == "held"
        assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 0


def test_provider_task_identifier_blocks_premature_cancellation_refund(
    ai_task_api,
) -> None:
    database, context, _sessions, client, reservation, attempt = ai_task_api
    state = AITaskStateService(database)
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"queued"},
        target_status="running",
    )
    state.transition_attempt(
        context,
        task_id=reservation.task_id,
        attempt_id=attempt.id,
        expected_statuses={"pending"},
        target_status="running",
    )
    state.record_provider_submission(
        context,
        task_id=reservation.task_id,
        attempt_id=attempt.id,
        provider_task_id="provider-task-100",
        billable_acknowledged=False,
    )

    response = client.post(
        f"/ai/tasks/{reservation.task_id}/cancel",
        headers=_headers(context.workspace_id),
    )

    assert response.status_code == 200
    assert response.json()["cancellation_outcome"] == "requested"
    with database.session_factory() as session:
        hold = session.get(TicketHoldRecord, int(reservation.hold_id))
        assert hold is not None and hold.status == "held"


def test_running_task_releases_only_after_explicit_nonbillable_confirmation(
    ai_task_api,
) -> None:
    database, context, _sessions, client, reservation, attempt = ai_task_api
    state = AITaskStateService(database)
    state.transition_task(
        context,
        task_id=reservation.task_id,
        expected_statuses={"queued"},
        target_status="running",
    )
    state.transition_attempt(
        context,
        task_id=reservation.task_id,
        attempt_id=attempt.id,
        expected_statuses={"pending"},
        target_status="running",
    )
    with database.session_factory.begin() as session:
        record = session.get(AITaskAttemptRecord, int(attempt.id))
        assert record is not None
        record.diagnostic = {"billing_state": "not_billable"}

    response = client.post(
        f"/ai/tasks/{reservation.task_id}/cancel",
        headers=_headers(context.workspace_id),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["released_microtickets"] == "1000000"
