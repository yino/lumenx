from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_cloud_model_setting_routes_are_closed_and_hidden() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "LUMENX_DEPLOYMENT_MODE": "cloud",
            "LUMENX_DATABASE_URL": (
                "postgresql+psycopg://lumenx:test@localhost:5432/lumenx_test"
            ),
            "LUMENX_REDIS_URL": "redis://localhost:6379/15",
            "LUMENX_OSS_ENDPOINT": "oss.example.invalid",
            "LUMENX_OSS_BUCKET_NAME": "lumenx-test-private",
            "LUMENX_OSS_ACCESS_KEY_ID": "test-access-key",
            "LUMENX_OSS_ACCESS_KEY_SECRET": "test-access-secret",
            "LUMENX_SESSION_SECRET": "0123456789abcdef0123456789abcdef",
            "LUMENX_MODEL_CATALOG_PATH": str(
                PROJECT_ROOT / "config/model_catalog/generated/model_catalog.json"
            ),
            "LUMENX_PROVIDER_SECRET_REFS": "LUMENX_TEST_PROVIDER_SECRET",
            "LUMENX_TEST_PROVIDER_SECRET": "test-provider-secret",
        }
    )
    script = textwrap.dedent(
        """
        from fastapi.testclient import TestClient

        from src.apps.comic_gen.api import app, pipeline

        assert app.state.deployment_settings.deployment_mode.value == "cloud"
        assert type(pipeline).__name__ == "_CloudLegacyPipelineGuard"
        paths = app.openapi()["paths"]
        assert "/series/{series_id}/model_settings" not in paths
        assert "/projects/{script_id}/model_settings" not in paths
        for sensitive_path in (
            "/debug/config",
            "/diagnose/log_tail",
            "/system/check",
            "/config/info",
            "/config/env",
            "/config/mulerun-login",
        ):
            assert sensitive_path not in paths

        client = TestClient(app)
        requests = (
            ("get", "/series/missing/model_settings", None),
            ("put", "/series/missing/model_settings", {}),
            ("post", "/projects/missing/model_settings", {}),
        )
        for method, path, payload in requests:
            request = getattr(client, method)
            response = request(path) if payload is None else request(path, json=payload)
            assert response.status_code == 404, (method, path, response.text)
            assert response.json()["code"] == "MODEL_SETTINGS_UNAVAILABLE"
            assert response.json()["correlation_id"]

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["deployment"] == "cloud"
        assert "log_file" not in health.json()
        assert "log_dir" not in health.json()

        sensitive_requests = (
            ("get", "/debug/config", None),
            ("get", "/diagnose/log_tail", None),
            ("get", "/system/check", None),
            ("get", "/config/info", None),
            ("get", "/config/env", None),
            ("post", "/config/env", {}),
            ("post", "/config/mulerun-login", None),
        )
        for method, path, payload in sensitive_requests:
            request = getattr(client, method)
            response = request(path) if payload is None else request(path, json=payload)
            assert response.status_code == 404, (method, path, response.text)
            assert response.json()["code"] == "DESKTOP_ENDPOINT_UNAVAILABLE"
            assert response.json()["correlation_id"]
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
