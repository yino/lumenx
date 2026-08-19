from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.platform.ai_gateway import AIGatewayResourceNotFoundError
from src.platform.ai_gateway_contract import AIGatewayContractError, AIGatewayRequestContract
from src.platform.auth.protection import CookieSecurityMiddleware
from src.platform.content_repositories import ScopedDocumentNotFoundError
from src.platform.database import _reset_postgres_connection, set_transaction_user_context
from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    TicketHoldRecord,
    TicketLedgerRecord,
    UsageEventRecord,
    WorkspaceRecord,
)
from src.platform.media_storage import CloudMediaStorage
from src.platform.ticket_settlement import TicketSettlementService
from tests.test_ai_gateway import RecordingDispatcher, _gateway, _gateway_database, _payload
from tests.test_ai_gateway_contract import _payload as _contract_payload
from tests.test_ai_recovery import RecordingRecoveryInvoker, _recovery_service
from tests.test_ai_worker import RecordingClientFactory, RecordingInvoker, _worker
from tests.test_content_repositories import _create_scope
from tests.test_media_storage import FakePrivateObjectStore


def test_foreign_ids_cannot_create_tasks_or_issue_signed_urls() -> None:
    database, owner, media_id, asset_id = _gateway_database()
    foreign_user = _create_scope(database)
    with database.session_factory.begin() as session:
        workspace = WorkspaceRecord(
            user_id=int(owner.identity.user_id),
            name="其他工作区",
        )
        session.add(workspace)
        session.flush()
        other_workspace_id = workspace.id
    same_user_other_workspace = replace(owner, workspace_id=str(other_workspace_id))
    object_store = FakePrivateObjectStore()
    storage = CloudMediaStorage(database, object_store)
    gateway = _gateway(database, RecordingDispatcher())
    try:
        for attacker in (foreign_user, same_user_other_workspace):
            with pytest.raises(ScopedDocumentNotFoundError):
                storage.authorized_url(
                    attacker,
                    str(media_id),
                    datetime.now(UTC) + timedelta(minutes=5),
                )
            with pytest.raises(AIGatewayResourceNotFoundError):
                gateway.submit(attacker, _payload(media_id, asset_id))

        assert object_store.signed == []
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 1
    finally:
        database.engine.dispose()


def test_cookie_and_gateway_boundaries_reject_csrf_paths_endpoints_and_credentials() -> None:
    app = FastAPI()
    app.add_middleware(
        CookieSecurityMiddleware,
        allowed_origins=frozenset({"https://studio.example.com"}),
    )

    @app.post("/mutate")
    def mutate():
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set("lumenx_session", "opaque-session")
    client.cookies.set("lumenx_csrf", "csrf-token")
    try:
        assert client.post(
            "/mutate",
            headers={"origin": "https://evil.example", "x-csrf-token": "csrf-token"},
        ).json()["code"] == "ORIGIN_DENIED"
        assert client.post(
            "/mutate",
            headers={"origin": "https://studio.example.com", "x-csrf-token": "wrong"},
        ).json()["code"] == "CSRF_INVALID"
    finally:
        client.close()

    database, context, _media_id, _asset_id = _gateway_database()
    try:
        attacks = (
            _contract_payload(content={"file_path": "/etc/passwd"}),
            _contract_payload(content={"object_key": "users/other/private.png"}),
            _contract_payload(parameters={"endpoint_url": "https://evil.example/api"}),
            _contract_payload(content={"authorization": "Bearer provider-secret-value"}),
        )
        for attack in attacks:
            with pytest.raises(AIGatewayContractError):
                AIGatewayRequestContract.parse(context, attack)
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 0
    finally:
        database.engine.dispose()


def test_provider_retry_ambiguity_never_resubmits_or_double_charges() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _gateway(database, RecordingDispatcher()).submit(
        context,
        _payload(media_id, asset_id),
    )
    secret = "sk-provider-secret-that-must-not-persist"
    invoker = RecordingInvoker(error=RuntimeError(f"Authorization: Bearer {secret}"))
    worker = _worker(database, RecordingClientFactory(), invoker)
    recovery_invoker = RecordingRecoveryInvoker()
    recovery = _recovery_service(database, recovery_invoker, stale_after_seconds=0)
    try:
        ambiguous = worker.execute(submitted.task_id)
        duplicate_delivery = worker.execute(submitted.task_id)

        assert ambiguous.status == "ambiguous"
        assert duplicate_delivery.acquired is False
        assert invoker.task_ids == [submitted.task_id]
        with database.session_factory() as session:
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            task = session.get(AITaskRecord, int(submitted.task_id))
            hold = session.scalar(
                select(TicketHoldRecord).where(
                    TicketHoldRecord.task_id == int(submitted.task_id)
                )
            )
            assert attempt.status == "ambiguous"
            assert secret not in str(attempt.diagnostic)
            assert task.status == "running"
            assert hold.status == "held"
            assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 0

        recovered = recovery.recover(submitted.task_id)
        duplicate_recovery = recovery.recover(submitted.task_id)
        assert recovered.status == "provider_succeeded"
        assert duplicate_recovery.recovered is False
        assert len(recovery_invoker.calls) == 1

        settlement = TicketSettlementService(database)
        first = settlement.settle_success(
            context,
            task_id=submitted.task_id,
            attempt_id=submitted.attempt_id,
            raw_provider_usage={"image_count": 1},
        )
        second = settlement.settle_success(
            context,
            task_id=submitted.task_id,
            attempt_id=submitted.attempt_id,
            raw_provider_usage={"image_count": 999},
        )

        assert second.reused is True
        assert second.usage_event_id == first.usage_event_id
        assert second.charged_microtickets == first.charged_microtickets
        with database.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(AITaskRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketHoldRecord)) == 1
            assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TicketLedgerRecord)) == 3
    finally:
        database.engine.dispose()


def test_rls_connection_reuse_resets_previous_identity_before_next_request() -> None:
    first_session = Mock()
    second_session = Mock()
    connection = Mock()
    first_identity = SimpleNamespace(
        user_id="1",
        session_id="1",
    )
    second_identity = SimpleNamespace(
        user_id="2",
        session_id="2",
    )

    set_transaction_user_context(first_session, first_identity)
    _reset_postgres_connection(
        connection,
        None,
        SimpleNamespace(terminate_only=False),
    )
    set_transaction_user_context(second_session, second_identity)

    assert first_session.execute.call_args.args[1]["user_id"] != second_session.execute.call_args.args[1]["user_id"]
    connection.cursor.return_value.execute.assert_called_once_with("RESET ALL")
    assert connection.rollback.call_count == 2
