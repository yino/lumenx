from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.platform.configuration_schemas import (
    ImageTokenFormula,
    LLMTokenFormula,
    SpeechTokenFormula,
    VideoTokenFormula,
)
from src.platform.metering import (
    MAX_METERING_TOKENS,
    MeteringValidationError,
    evaluate_metering_tokens,
    maximum_metering_tokens,
)


def test_llm_metering_weights_actual_input_and_output_tokens() -> None:
    formula = LLMTokenFormula(
        kind="llm",
        input_weight=1,
        output_weight=2,
        max_input_tokens=1000,
        max_output_tokens=500,
    )

    assert evaluate_metering_tokens(
        formula,
        raw_usage={"input_tokens": 100, "output_tokens": 50},
    ) == 200
    assert maximum_metering_tokens(formula) == 2000

    with pytest.raises(MeteringValidationError, match="超出任务配置快照"):
        evaluate_metering_tokens(
            formula,
            raw_usage={"input_tokens": 1001, "output_tokens": 1},
        )

    assert evaluate_metering_tokens(
        formula.model_dump(mode="json"),
        raw_usage={"input_tokens": 100, "output_tokens": 50},
    ) == 200


def test_image_metering_uses_count_resolution_and_selected_options() -> None:
    formula = ImageTokenFormula(
        kind="image",
        base_tokens=10,
        per_image_tokens=100,
        max_images=4,
        resolution_multipliers={"1024x1024": 1, "2048x2048": 2},
        option_multipliers={"premium": 3, "quality=high": 2},
    )

    assert evaluate_metering_tokens(
        formula,
        parameters={
            "count": 2,
            "resolution": "2048x2048",
            "premium": True,
            "quality": "high",
        },
    ) == 2410

    with pytest.raises(MeteringValidationError, match="输出数量"):
        evaluate_metering_tokens(
            formula,
            parameters={"count": 5, "resolution": "1024x1024"},
        )
    with pytest.raises(MeteringValidationError, match="分辨率"):
        evaluate_metering_tokens(
            formula,
            parameters={"count": 1, "resolution": "8k"},
        )


def test_video_metering_uses_duration_outputs_resolution_and_audio() -> None:
    formula = VideoTokenFormula(
        kind="video",
        base_tokens=10,
        tokens_per_second=100,
        max_duration_seconds=10,
        max_outputs=2,
        resolution_multipliers={"720p": 1, "1080p": 2},
        audio_multiplier=3,
    )

    assert evaluate_metering_tokens(
        formula,
        parameters={
            "duration": 5,
            "output_count": 2,
            "resolution": "1080p",
            "generate_audio": True,
        },
    ) == 6120
    assert evaluate_metering_tokens(
        formula,
        parameters={"duration": 1.2, "output_count": 1, "resolution": "720p"},
    ) == 210


@pytest.mark.parametrize(
    ("unit", "usage", "expected"),
    [
        ("characters", {"characters": 101}, 202),
        ("provider_tokens", {"provider_tokens": 80}, 160),
        ("seconds", {"seconds": 1.2}, 4),
    ],
)
def test_speech_metering_supports_configured_provider_units(
    unit: str,
    usage: dict,
    expected: int,
) -> None:
    formula = SpeechTokenFormula(
        kind="speech",
        unit=unit,
        tokens_per_unit=2,
        max_units=1000,
    )

    assert evaluate_metering_tokens(formula, raw_usage=usage) == expected
    assert maximum_metering_tokens(formula) == 2000


def test_metering_rejects_negative_boolean_and_overflow_values() -> None:
    formula = SimpleNamespace(
        kind="llm",
        input_weight=MAX_METERING_TOKENS,
        output_weight=1,
        max_input_tokens=MAX_METERING_TOKENS,
        max_output_tokens=1,
    )

    for usage in (
        {"input_tokens": -1, "output_tokens": 0},
        {"input_tokens": True, "output_tokens": 0},
    ):
        with pytest.raises(MeteringValidationError):
            evaluate_metering_tokens(formula, raw_usage=usage)

    with pytest.raises(MeteringValidationError, match="溢出"):
        evaluate_metering_tokens(
            formula,
            raw_usage={"input_tokens": 2, "output_tokens": 0},
        )
