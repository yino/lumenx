from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from src.platform.error_protocol import install_cloud_error_protocol
from src.platform.observability import metrics


class Payload(BaseModel):
    name: str = Field(min_length=2)


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/domain-error")
    def domain_error() -> None:
        raise HTTPException(
            status_code=409,
            detail={"code": "CONTENT_VERSION_CONFLICT", "message": "内容版本已变化"},
        )

    @app.get("/unsafe-error")
    def unsafe_error() -> None:
        raise HTTPException(status_code=503, detail="provider stack trace")

    @app.post("/validate")
    def validate(payload: Payload) -> Payload:
        return payload

    @app.get("/crash")
    def crash() -> None:
        raise RuntimeError("secret provider diagnostics")

    @app.get("/admin/probe")
    def admin_probe() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/admin/fail")
    def admin_fail() -> None:
        raise HTTPException(status_code=409, detail="管理员请求冲突")

    install_cloud_error_protocol(app)
    return TestClient(app, raise_server_exceptions=False)


def test_error_protocol_preserves_domain_code_and_correlation_id() -> None:
    response = _client().get(
        "/domain-error",
        headers={"X-Correlation-ID": "request-test-100"},
    )

    assert response.status_code == 409
    assert response.headers["X-Correlation-ID"] == "request-test-100"
    assert response.json() == {
        "code": "CONTENT_VERSION_CONFLICT",
        "message": "内容版本已变化",
        "correlation_id": "request-test-100",
    }


def test_error_protocol_projects_validation_and_unknown_errors_safely() -> None:
    validation = _client().post("/validate", json={"name": ""})
    unsafe = _client().get("/unsafe-error")
    crash = _client().get("/crash")

    assert validation.status_code == 422
    assert validation.json()["code"] == "VALIDATION_ERROR"
    assert validation.json()["message"] == "提交的信息不符合要求，请检查后重试"
    assert validation.json()["correlation_id"] == validation.headers["X-Correlation-ID"]

    assert unsafe.json()["code"] == "SERVICE_UNAVAILABLE"
    assert unsafe.json()["message"] == "服务暂时不可用，请稍后重试"
    assert "provider" not in str(unsafe.json()).lower()

    assert crash.status_code == 500
    assert crash.json()["code"] == "INTERNAL_ERROR"
    assert crash.json()["message"] == "服务暂时不可用，请稍后重试"
    assert "secret" not in str(crash.json()).lower()


def test_error_protocol_replaces_invalid_correlation_header() -> None:
    response = _client().get(
        "/domain-error",
        headers={"X-Correlation-ID": "contains spaces and should be rejected"},
    )

    correlation_id = response.headers["X-Correlation-ID"]
    assert correlation_id != "contains spaces and should be rejected"
    assert response.json()["correlation_id"] == correlation_id


def test_error_protocol_records_bounded_admin_latency_and_error_metrics() -> None:
    metrics.reset()
    client = _client()

    assert client.get("/admin/probe").status_code == 200
    assert client.get("/admin/fail").status_code == 409

    snapshot = metrics.snapshot()
    counters = {
        (item["name"], tuple(sorted(item["labels"].items()))): item["value"]
        for item in snapshot["counters"]
    }
    assert counters[
        (
            "admin_api_requests_total",
            (("operation", "get"), ("outcome", "succeeded"), ("status", "200")),
        )
    ] == 1
    assert counters[
        (
            "admin_api_requests_total",
            (("operation", "get"), ("outcome", "error"), ("status", "409")),
        )
    ] == 1
    admin_histograms = [
        item
        for item in snapshot["histograms"]
        if item["name"] == "admin_api_request_duration_seconds"
    ]
    assert {item["labels"]["outcome"] for item in admin_histograms} == {
        "succeeded",
        "error",
    }
    assert all(item["count"] == 1 and item["sum"] >= 0 for item in admin_histograms)
