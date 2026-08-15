from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from src.platform.edge import LegacyCloudAPICompatibilityMiddleware
from src.platform.observability import MetricsRegistry


def test_cloud_frontend_and_nginx_use_one_versioned_api_subtree() -> None:
    api_source = Path("frontend/src/lib/api.ts").read_text(encoding="utf-8")
    nginx = Path("docker/nginx.conf").read_text(encoding="utf-8")

    assert "`${window.location.origin}/api/v1`" in api_source
    assert "location /api/v1/" in nginx
    assert "proxy_pass http://backend:17177/;" in nginx
    assert "try_files" not in nginx.split("location /api/v1/", 1)[1].split("}", 1)[0]
    for omitted_root in ("/auth", "/workspaces", "/wallet", "/admin", "/media", "/playground"):
        assert f"location {omitted_root}" not in nginx


def test_cloud_compose_keeps_backend_internal_and_trusts_only_private_proxy_hop() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    backend = compose.split("  backend:", 1)[1].split("  ai-worker:", 1)[0]
    dockerfile = Path("docker/Dockerfile.backend").read_text(encoding="utf-8")

    assert not Path("Dockerfile.backend").exists()
    assert 'expose:\n      - "17177"' in backend
    assert "\n    ports:" not in backend
    assert '"--proxy-headers"' in dockerfile
    assert '"--forwarded-allow-ips=*"' in dockerfile


def test_trusted_proxy_normalizes_client_and_untrusted_peer_cannot_spoof() -> None:
    app = FastAPI()

    @app.get("/client")
    def client(request: Request) -> dict[str, str]:
        return {"host": request.client.host if request.client else "unknown"}

    trusted = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=["testclient"]))
    assert trusted.get(
        "/client",
        headers={"X-Forwarded-For": "203.0.113.10"},
    ).json() == {"host": "203.0.113.10"}

    untrusted = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=["10.0.0.2"]))
    assert untrusted.get(
        "/client",
        headers={"X-Forwarded-For": "203.0.113.99"},
    ).json() == {"host": "testclient"}


def test_legacy_edge_route_records_low_cardinality_compatibility_metric() -> None:
    registry = MetricsRegistry()
    app = FastAPI()

    @app.get("/auth/me")
    def current_user() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(LegacyCloudAPICompatibilityMiddleware, registry=registry)
    client = TestClient(app)
    assert client.get(
        "/auth/me",
        headers={"X-LumenX-Legacy-API": "1"},
    ).status_code == 200
    assert registry.snapshot()["counters"] == [
        {
            "name": "cloud_legacy_api_requests_total",
            "labels": {"operation": "edge_compatibility"},
            "value": 1.0,
        }
    ]


def test_legacy_edge_access_log_survives_frontend_container_replacement() -> None:
    generator = Path("docker/40-lumenx-api-compat.sh").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "mkdir -p \"${compat_dir}\" \"${compat_log_dir}\"" in generator
    assert "access_log /var/log/lumenx/lumenx-legacy-api.log combined;" in generator
    frontend = compose.split("  frontend:", 1)[1].split("\nvolumes:", 1)[0]
    assert "lumenx-nginx-legacy-logs:/var/log/lumenx" in frontend
    assert "lumenx-nginx-legacy-logs:" in compose.split("\nvolumes:", 1)[1]
