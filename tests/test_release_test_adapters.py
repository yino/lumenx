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
from src.models.provider_result import ProviderGenerationResult, ProviderTextResult
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


def test_production_r2v_polish_builds_bilingual_multimodal_request() -> None:
    task = SimpleNamespace(
        request_payload={
            "content": {
                "operation": "video.r2v_prompt.polish",
                "draft_prompt": "[character1:张成]望向酒店窗口",
                "slots": [
                    {"description": "张成：疲惫的普通男性"},
                    {"description": "劳斯莱斯驾驶室内"},
                ],
                "feedback": "增加缓慢推镜",
                "prev_cn": "张成望向酒店窗口。",
            },
        },
        provider_inputs=(
            SimpleNamespace(signed_url="https://media.example/reference.png"),
        ),
    )

    messages, response_format = ProductionProviderInvoker._text_request(task)

    assert response_format == {"type": "json_object"}
    assert messages[0]["role"] == "system"
    assert "character1: 张成：疲惫的普通男性" in messages[0]["content"]
    assert "character2: 劳斯莱斯驾驶室内" in messages[0]["content"]
    assert '"prompt_cn"' in messages[0]["content"]
    assert '"prompt_en"' in messages[0]["content"]
    assert messages[1]["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://media.example/reference.png"},
        },
        {
            "type": "text",
            "text": """[当前提示词-CN]
张成望向酒店窗口。

[当前提示词-EN]
[character1:张成]望向酒店窗口

[用户反馈]
增加缓慢推镜

请根据用户反馈同步修改双语版本，只修改用户指出的问题，保持其他部分不变。""",
        },
    ]


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


def test_production_text_runtime_enforces_metering_output_limit(
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
    captured = {}

    class Adapter:
        def chat_with_usage(self, messages, **parameters):
            captured.update(parameters)
            return ProviderTextResult(
                content='{"frames": []}',
                raw_usage={"input_tokens": 10, "output_tokens": 20},
                provider_request_id="request-1",
            )

    class Submission:
        billable_acknowledged = False

        def __call__(self, *_args):
            self.billable_acknowledged = True

    task = SimpleNamespace(
        task_id="17",
        attempt_id="17",
        capability="script.analysis",
        request_payload={
            "content": {"operation": "storyboard.analyze", "text": "剧本"},
        },
        model_route=SimpleNamespace(
            provider="dashscope",
            provider_model_id="qwen3.6-plus",
            metering_formula={"max_output_tokens": 8192},
        ),
    )

    outcome = ProductionProviderInvoker(settings).invoke(
        SimpleNamespace(adapter=Adapter()),
        task,
        Submission(),
    )

    assert captured["max_tokens"] == 8192
    assert captured["enable_thinking"] is False
    assert outcome.raw_usage == {"input_tokens": 10, "output_tokens": 20}


def test_production_speech_runtime_generates_one_controlled_output_per_item(
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
    calls: list[dict[str, object]] = []

    class Adapter:
        def synthesize(self, text, output_path, **parameters):
            calls.append({"text": text, **parameters})
            Path(output_path).write_bytes(
                b"RIFF" + (4).to_bytes(4, "little") + b"WAVEdata"
            )
            return output_path, 0.0, f"speech-request-{len(calls)}"

    class Submission:
        recorded = False

        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None, str | None]] = []

        def __call__(self, provider, provider_task_id, request_id):
            self.recorded = True
            self.calls.append((provider, provider_task_id, request_id))

    submission = Submission()
    task = SimpleNamespace(
        task_id="speech-task-1",
        attempt_id="speech-attempt-1",
        capability="speech.tts",
        request_payload={
            "content": {
                "operation": "audio.dialogue.batch",
                "items": [
                    {"text": "第一句", "voice_id": "longcheng_v2"},
                    {"text": "第二句台词", "voice_id": "longcheng_v2"},
                ],
            }
        },
        model_route=SimpleNamespace(
            provider="dashscope",
            provider_model_id="cosyvoice-v2",
            metering_formula={"max_units": 20_000},
        ),
    )

    outcome = ProductionProviderInvoker(settings).invoke(
        SimpleNamespace(adapter=Adapter()),
        task,
        submission,
    )

    assert [call["text"] for call in calls] == ["第一句", "第二句台词"]
    assert calls[0]["voice"] == "longcheng_v2"
    assert outcome.raw_usage == {"characters": 8}
    assert outcome.result["content"] == {
        "operation": "audio.dialogue.batch",
        "output_count": 2,
    }
    assert [item["content_type"] for item in outcome.result["outputs"]] == [
        "audio/wav",
        "audio/wav",
    ]
    assert submission.calls == [("dashscope", None, "speech-request-1")]
