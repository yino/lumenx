from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from src.platform.auth.admin_identity import AdminSessionPrincipal
from src.platform.auth.sessions import SessionAuthenticationError
from src.platform.observability import MetricsRegistry, StructuredEventLogger
from src.platform.observability_api import install_observability_api


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class FakeSessions:
    def resolve(self, token: str) -> AdminSessionPrincipal:
        assert token == "session-token"
        return AdminSessionPrincipal(
            admin_id=1,
            session_id=11,
            username="admin",
            must_change_password=False,
        )


def _metrics_client(*, admin_authenticated: bool) -> tuple[TestClient, MetricsRegistry]:
    registry = MetricsRegistry()
    registry.increment(
        "auth_results_total",
        labels={"action": "login", "outcome": "succeeded"},
    )
    app = FastAPI()
    install_observability_api(
        app,
        SimpleNamespace(admin_sessions=FakeSessions()),
        registry=registry,
    )
    @app.exception_handler(SessionAuthenticationError)
    def unauthenticated(_request: Request, exc: SessionAuthenticationError):
        return JSONResponse(status_code=401, content={"code": exc.code, "message": str(exc)})

    client = TestClient(app)
    client.cookies.set(
        "lumenx_admin_session" if admin_authenticated else "lumenx_session",
        "session-token",
    )
    return client, registry


def test_metrics_registry_uses_low_cardinality_bounded_series() -> None:
    registry = MetricsRegistry(max_series=2)
    registry.increment("requests_total", labels={"status": "ok"})
    for value in range(1000):
        registry.observe("request_seconds", value / 1000, labels={"operation": "login"})

    snapshot = registry.snapshot()
    assert snapshot["histograms"] == [
        {
            "name": "request_seconds",
            "labels": {"operation": "login"},
            "count": 1000,
            "sum": pytest.approx(499.5),
            "maximum": pytest.approx(0.999),
        }
    ]
    with pytest.raises(ValueError, match="安全上限"):
        registry.gauge("queue_depth", 1, labels={"queue": "ai"})


def test_metrics_reject_unapproved_and_high_cardinality_labels() -> None:
    registry = MetricsRegistry()
    with pytest.raises(ValueError, match="未批准字段"):
        registry.increment("requests_total", labels={"user_id": str(uuid.uuid4())})
    with pytest.raises(ValueError, match="资源标识"):
        registry.increment("requests_total", labels={"status": str(uuid.uuid4())})


def test_structured_events_allow_ids_but_reject_raw_content_and_secrets() -> None:
    logger = logging.getLogger(f"test-observability-{uuid.uuid4()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RecordingHandler()
    logger.addHandler(handler)
    event_logger = StructuredEventLogger(logger)
    task_id = str(uuid.uuid4())

    event_logger.emit(
        "ai.worker_succeeded",
        task_id=task_id,
        capability="text_to_image",
        metering_tokens=123,
    )

    assert json.loads(handler.messages[-1]) == {
        "capability": "text_to_image",
        "event": "ai.worker_succeeded",
        "metering_tokens": 123,
        "task_id": task_id,
    }
    with pytest.raises(ValueError, match="原始用户或供应商内容"):
        event_logger.emit("unsafe.prompt", prompt="用户剧本文本")
    with pytest.raises(ValueError, match="原始用户或供应商内容"):
        event_logger.emit("unsafe.payload", request_payload={"title": "正文"})
    with pytest.raises(ValueError, match="明文凭据字段"):
        event_logger.emit("unsafe.secret", api_key="sk-not-allowed-123456")


@pytest.mark.parametrize(
    "field",
    [
        "phone",
        "phone_canonical",
        "script",
        "prompt",
        "reset_credential",
        "object_key",
        "provider_diagnostics",
        "offline_reference",
        "idempotency_key",
    ],
)
def test_structured_events_reject_admin_private_fields_at_any_depth(field: str) -> None:
    event_logger = StructuredEventLogger(logging.getLogger("test-admin-redaction"))

    with pytest.raises(ValueError, match="原始用户或供应商内容"):
        event_logger.emit(
            "admin.unsafe",
            metadata={"nested": {field: "不得写入日志"}},
        )


def test_observability_metrics_are_platform_admin_only() -> None:
    admin_client, _registry = _metrics_client(admin_authenticated=True)
    response = admin_client.get("/admin/observability/metrics")
    assert response.status_code == 200
    assert response.json()["counters"] == [
        {
            "name": "auth_results_total",
            "labels": {"action": "login", "outcome": "succeeded"},
            "value": 1.0,
        }
    ]
    admin_client.close()

    user_client, _registry = _metrics_client(admin_authenticated=False)
    denied = user_client.get("/admin/observability/metrics")
    assert denied.status_code == 401
    assert "counters" not in denied.text
    user_client.close()
