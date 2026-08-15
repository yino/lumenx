from __future__ import annotations

import uuid

import pytest
from fastapi import Request
from sqlalchemy import select

from src.platform.audit import AuditService
from src.platform.db_models import AuditEventRecord
from tests.test_content_repositories import RepositoryDatabase, _create_scope


def _request(correlation_id: str = "audit-correlation-1") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/test",
            "headers": [(b"x-correlation-id", correlation_id.encode("ascii"))],
            "client": ("127.0.0.1", 50000),
        }
    )
    return request


def test_audit_service_records_safe_scoped_summary_and_correlation() -> None:
    database = RepositoryDatabase()
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)
    service = AuditService(database)

    service.record(
        context.identity,
        action="media.delete",
        target_type="media_object",
        target_id=str(uuid.uuid4()),
        request=_request(),
        workspace_id=context.workspace_id,
        after={"lifecycle_state": "deleted"},
    )

    with database.session_factory() as session:
        event = session.scalar(select(AuditEventRecord))
        assert event.actor_user_id == int(context.identity.user_id)
        assert event.workspace_id == int(context.workspace_id)
        assert event.correlation_id == "audit-correlation-1"
        assert event.after_summary == {"lifecycle_state": "deleted"}
        assert event.network_fingerprint
        assert event.created_at is not None
    database.engine.dispose()


@pytest.mark.parametrize(
    "summary",
    [
        {"password": "plaintext"},
        {"request_payload": {"story": "private"}},
        {"api_key": "sk-a-very-secret-key"},
    ],
)
def test_audit_service_rejects_sensitive_or_raw_content(summary) -> None:
    database = RepositoryDatabase()
    AuditEventRecord.__table__.create(database.engine)
    context = _create_scope(database)

    with pytest.raises(ValueError, match="不允许"):
        AuditService(database).record(
            context.identity,
            action="unsafe",
            target_type="test",
            target_id=None,
            request=_request(),
            after=summary,
        )
    database.engine.dispose()
