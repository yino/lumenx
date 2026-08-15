from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.platform.ai_io import ProviderOutputReference
from src.platform.provider_runtime import (
    ProductionProviderInvoker,
    ProductionProviderOutputDownloader,
)
from src.models.provider_result import ProviderGenerationResult
from src.platform.test_adapters import (
    DeterministicModelClientFactory,
    DeterministicPrivateObjectStore,
    DeterministicProviderInvoker,
    DeterministicProviderOutputDownloader,
)
from src.platform.settings import DeploymentSettings


def _settings(tmp_path: Path) -> DeploymentSettings:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    return DeploymentSettings(
        _env_file=None,
        deployment_mode="test",
        test_adapters_enabled=True,
        object_store_adapter="deterministic",
        provider_adapter="deterministic",
        test_signing_secret="release-test-signing-secret-value",
        test_object_store_root=tmp_path / "objects",
        database_url="postgresql+psycopg://lumenx:test@postgres/lumenx",
        redis_url="redis://redis:6379/0",
        session_secret="s" * 32,
        model_catalog_path=catalog,
    )


def test_deterministic_object_store_is_private_and_signed(tmp_path: Path) -> None:
    store = DeterministicPrivateObjectStore(_settings(tmp_path))
    store.put("users/u/workspaces/w/test.png", b"content", "image/png")
    url = store.signed_get_url("users/u/workspaces/w/test.png", 30)
    query = dict(item.split("=", 1) for item in url.split("?", 1)[1].split("&"))

    path = store.verify(
        "users/u/workspaces/w/test.png",
        int(query["expires"]),
        query["signature"],
    )
    assert path.read_bytes() == b"content"
    with pytest.raises(ValueError, match="签名无效"):
        store.verify(
            "users/u/workspaces/w/test.png",
            int(time.time()) + 30,
            "0" * 64,
        )


def test_deterministic_provider_produces_metered_private_output(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = SimpleNamespace(
        provider="deterministic",
        parameters={"count": 1, "resolution": "1024x1024"},
    )
    task = SimpleNamespace(
        task_id="00000000-0000-0000-0000-000000000001",
        attempt_id="00000000-0000-0000-0000-000000000002",
        capability="image.t2i",
        model_route=route,
        request_payload={"content": "测试画面"},
    )
    submissions = []
    client = DeterministicModelClientFactory(settings).create(route)

    outcome = DeterministicProviderInvoker(settings).invoke(
        client,
        task,
        lambda *args: submissions.append(args),
    )
    output = outcome.result["outputs"][0]
    downloaded = DeterministicProviderOutputDownloader(settings).download(
        "deterministic",
        ProviderOutputReference(
            url=output["url"],
            filename=output["filename"],
            declared_content_type=output["content_type"],
        ),
    )

    assert submissions
    assert outcome.raw_usage == {"output_count": 1, "resolution": "1024x1024"}
    assert downloaded.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_production_provider_runtime_uses_controlled_shared_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "server-secret")
    settings = DeploymentSettings(
        _env_file=None,
        deployment_mode="cloud",
        database_url="postgresql+psycopg://lumenx:test@postgres/lumenx",
        redis_url="redis://redis:6379/0",
        oss_endpoint="oss.example.invalid",
        oss_bucket_name="private",
        oss_access_key_id="id",
        oss_access_key_secret="secret",
        session_secret="s" * 32,
        model_catalog_path=catalog,
        provider_secret_refs="DASHSCOPE_API_KEY",
        provider_output_root=tmp_path / "provider-results",
    )

    class Adapter:
        def generate_with_usage(self, prompt, output_path, **parameters):
            assert prompt == "生成测试图片"
            assert parameters["model"] == "provider-image-v1"
            Path(output_path).write_bytes(b"\x89PNG\r\n\x1a\nproduction")
            return ProviderGenerationResult(
                output_path=output_path,
                elapsed_seconds=0.1,
                raw_usage={"output_count": 1, "resolution": "1024x1024"},
            )

    class Client:
        adapter = Adapter()

        @staticmethod
        def execution_parameters():
            return {"model": "provider-image-v1"}

    class Submission:
        recorded = False

        def __call__(self, *_args):
            self.recorded = True

    task = SimpleNamespace(
        task_id="00000000-0000-0000-0000-000000000010",
        attempt_id="00000000-0000-0000-0000-000000000011",
        capability="image.t2i",
        request_payload={"content": "生成测试图片"},
        model_route=SimpleNamespace(
            provider="dashscope",
            provider_model_id="provider-image-v1",
        ),
        provider_inputs=(),
    )
    outcome = ProductionProviderInvoker(settings).invoke(Client(), task, Submission())
    reference = outcome.result["outputs"][0]
    downloaded = ProductionProviderOutputDownloader(settings).download(
        "dashscope",
        ProviderOutputReference(
            url=reference["url"],
            filename=reference["filename"],
            declared_content_type=reference["content_type"],
        ),
    )

    assert downloaded.content.startswith(b"\x89PNG")
    assert outcome.raw_usage["output_count"] == 1
