from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_desktop_artifact import (
    configured_secret_values,
    scan_desktop_artifact,
)
from src.platform.settings import DeploymentSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_desktop_build_configuration_excludes_environment_and_source_tree() -> None:
    build_files = (
        PROJECT_ROOT / "build_mac.sh",
        PROJECT_ROOT / "build_windows.ps1",
        PROJECT_ROOT / "build.spec.template",
    )
    for path in build_files:
        content = path.read_text(encoding="utf-8")
        assert "datas.append(('.env', '.'))" not in content
        assert '--add-data "src:src"' not in content
        assert '"--add-data", "src;src"' not in content


def test_packaged_entrypoint_locks_desktop_mode_before_api_import() -> None:
    main_source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    lock_position = main_source.index('os.environ["LUMENX_DEPLOYMENT_MODE"] = "desktop"')
    api_import_position = main_source.index("from src.apps.comic_gen.api import app")
    assert lock_position < api_import_position


def test_desktop_build_marker_rejects_cloud_configuration() -> None:
    with pytest.raises(ValueError, match="桌面构建产物禁止启用云端部署模式"):
        DeploymentSettings(
            _env_file=None,
            desktop_build=True,
            deployment_mode="cloud",
        )


def test_artifact_scan_detects_environment_file_cloud_marker_and_secret(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "LumenX Studio.app"
    artifact.mkdir()
    (artifact / ".env").write_text("LUMENX_DEPLOYMENT_MODE=cloud", encoding="utf-8")
    (artifact / "binary").write_bytes(b"prefix-platform-secret-suffix")

    findings = scan_desktop_artifact(
        artifact,
        secret_values=(b"platform-secret",),
    )

    assert any("环境配置文件" in finding for finding in findings)
    assert any("云端运行标记" in finding for finding in findings)
    assert any("平台密钥字节" in finding for finding in findings)


def test_artifact_scan_accepts_local_only_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "LumenX Studio.app"
    artifact.mkdir()
    (artifact / "runtime.bin").write_bytes(b"LUMENX_DESKTOP_BUILD=true")
    assert scan_desktop_artifact(artifact, secret_values=(b"server-secret",)) == []


def test_configured_secret_values_ignores_placeholders() -> None:
    assert configured_secret_values(
        {
            "LUMENX_PROVIDER_SECRET_REFS": "CUSTOM_PROVIDER_SECRET",
            "CUSTOM_PROVIDER_SECRET": "real-secret-value",
            "DASHSCOPE_API_KEY": "your_dashscope_api_key_here",
        }
    ) == (b"real-secret-value",)


def test_desktop_entrypoint_overrides_inherited_cloud_mode() -> None:
    script = """
import os
import runpy
import sys
import types

sys.modules['webview'] = types.SimpleNamespace()
try:
    runpy.run_path('main.py', run_name='desktop_build_probe')
except Exception:
    pass
assert os.environ['LUMENX_DESKTOP_BUILD'] == 'true'
assert os.environ['LUMENX_DEPLOYMENT_MODE'] == 'desktop'
"""
    environment = os.environ.copy()
    environment["LUMENX_DEPLOYMENT_MODE"] = "cloud"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
