from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from src.platform.observability import StructuredEventLogger
from src.platform.provider_runtime import (
    _emit_provider_request_log,
    summarize_provider_parameters,
)


class RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_provider_parameter_summary_keeps_diagnostic_values_and_redacts_content() -> None:
    summary = summarize_provider_parameters(
        {
            "model": "gpt-image-2",
            "size": "1024x1536",
            "quality": "high",
            "resolution": "720p",
            "duration": 5,
            "prompt": "用户不应原样进入日志",
            "ref_image_paths": ["https://signed.example/image.png"],
            "authorization": "Bearer provider-secret",
            "on_provider_ids": lambda *_args: None,
        }
    )

    encoded = json.dumps(summary, ensure_ascii=False)
    assert summary["model"] == "gpt-image-2"
    assert summary["size"] == "1024x1536"
    assert summary["duration"] == 5
    assert summary["prompt_summary"]["chars"] == 10
    assert summary["ref_image_paths_summary"] == {
        "redacted": True,
        "count": 1,
    }
    assert summary["authorization_redacted"] == {
        "redacted": True,
        "present": True,
    }
    assert "用户不应原样进入日志" not in encoded
    assert "provider-secret" not in encoded
    assert "signed.example" not in encoded
    assert "on_provider_ids" not in encoded


def test_provider_request_log_contains_route_identity_and_safe_summaries(monkeypatch) -> None:
    logger = logging.getLogger("test-provider-request-log")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RecordingHandler()
    logger.addHandler(handler)
    monkeypatch.setattr(
        "src.platform.provider_runtime.events",
        StructuredEventLogger(logger),
    )

    task = SimpleNamespace(
        task_id="task-123",
        attempt_id="attempt-456",
        capability="video.i2v",
        model_route=SimpleNamespace(provider="xlinks"),
    )
    _emit_provider_request_log(
        task,
        model_id="grok-imagine-video",
        parameters={"duration": 5, "resolution": "720p"},
        prompt="生成一个镜头",
        mode="i2v",
        input_count=1,
    )

    payload = json.loads(handler.messages[-1])
    assert payload["event"] == "ai.provider_request"
    assert payload["task_id"] == "task-123"
    assert payload["attempt_id"] == "attempt-456"
    assert payload["capability"] == "video.i2v"
    assert payload["provider"] == "xlinks"
    assert payload["provider_model_id"] == "grok-imagine-video"
    assert payload["mode"] == "i2v"
    assert payload["input_count"] == 1
    assert payload["parameter_summary"] == {
        "duration": 5,
        "resolution": "720p",
    }
    assert payload["prompt_summary"]["chars"] == 6
    assert "生成一个镜头" not in handler.messages[-1]
