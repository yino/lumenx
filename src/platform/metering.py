from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


MAX_METERING_TOKENS = (1 << 63) - 1


class MeteringValidationError(ValueError):
    pass


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MeteringValidationError(f"{name}必须是整数")
    if value < minimum:
        raise MeteringValidationError(f"{name}不能小于 {minimum}")
    if value > MAX_METERING_TOKENS:
        raise MeteringValidationError(f"{name}超出可计量范围")
    return value


def _units(value: Any, name: str, *, allow_fraction: bool = False) -> int:
    if allow_fraction:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise MeteringValidationError(f"{name}必须是非负有限数值")
        result = math.ceil(float(value))
        if result > MAX_METERING_TOKENS:
            raise MeteringValidationError(f"{name}超出可计量范围")
        return result
    return _integer(value, name)


def _checked_add(left: int, right: int) -> int:
    result = left + right
    if result > MAX_METERING_TOKENS:
        raise MeteringValidationError("计量 token 计算结果溢出")
    return result


def _checked_multiply(*values: int) -> int:
    result = 1
    for value in values:
        result *= value
        if result > MAX_METERING_TOKENS:
            raise MeteringValidationError("计量 token 计算结果溢出")
    return result


def _formula_value(formula: Any, name: str) -> Any:
    if isinstance(formula, Mapping):
        if name not in formula:
            raise MeteringValidationError(f"计量公式缺少字段：{name}")
        return formula[name]
    try:
        return getattr(formula, name)
    except AttributeError as exc:
        raise MeteringValidationError(f"计量公式缺少字段：{name}") from exc


def _output_count(parameters: Mapping[str, Any], maximum: int) -> int:
    raw = parameters.get("output_count", parameters.get("count", 1))
    count = _integer(raw, "输出数量", minimum=1)
    if count > maximum:
        raise MeteringValidationError("输出数量超出计量公式上限")
    return count


def _resolution_multiplier(
    formula: Any,
    parameters: Mapping[str, Any],
) -> int:
    multipliers = dict(_formula_value(formula, "resolution_multipliers"))
    resolution = parameters.get("resolution", parameters.get("size"))
    if resolution is None and len(multipliers) == 1:
        resolution = next(iter(multipliers))
    if not isinstance(resolution, str) or resolution not in multipliers:
        raise MeteringValidationError("分辨率不在计量公式范围内")
    return _integer(multipliers[resolution], "分辨率倍率", minimum=1)


def _option_multiplier(
    formula: Any,
    parameters: Mapping[str, Any],
) -> int:
    result = 1
    for selector, raw_multiplier in dict(
        _formula_value(formula, "option_multipliers")
    ).items():
        multiplier = _integer(raw_multiplier, f"选项 {selector} 倍率", minimum=1)
        if "=" in selector:
            name, expected = selector.split("=", 1)
            selected = str(parameters.get(name)) == expected
        else:
            selected = bool(parameters.get(selector, False))
        if selected:
            result = _checked_multiply(result, multiplier)
    return result


def evaluate_llm_metering_tokens(
    formula: Any,
    raw_usage: Mapping[str, Any],
) -> int:
    input_tokens = _integer(raw_usage.get("input_tokens"), "输入 token")
    output_tokens = _integer(raw_usage.get("output_tokens"), "输出 token")
    max_input = _integer(_formula_value(formula, "max_input_tokens"), "最大输入 token", minimum=1)
    max_output = _integer(_formula_value(formula, "max_output_tokens"), "最大输出 token", minimum=1)
    if input_tokens > max_input or output_tokens > max_output:
        raise MeteringValidationError("供应商 token 用量超出任务配置快照上限")
    input_charge = _checked_multiply(
        input_tokens,
        _integer(_formula_value(formula, "input_weight"), "输入 token 权重"),
    )
    output_charge = _checked_multiply(
        output_tokens,
        _integer(_formula_value(formula, "output_weight"), "输出 token 权重", minimum=1),
    )
    return _checked_add(input_charge, output_charge)


def evaluate_image_metering_tokens(
    formula: Any,
    parameters: Mapping[str, Any],
) -> int:
    count = _output_count(
        parameters,
        _integer(_formula_value(formula, "max_images"), "最大图片数量", minimum=1),
    )
    variable = _checked_multiply(
        _integer(_formula_value(formula, "per_image_tokens"), "每张图片 token", minimum=1),
        count,
        _resolution_multiplier(formula, parameters),
        _option_multiplier(formula, parameters),
    )
    return _checked_add(
        _integer(_formula_value(formula, "base_tokens"), "图片基础 token"),
        variable,
    )


def evaluate_video_metering_tokens(
    formula: Any,
    parameters: Mapping[str, Any],
) -> int:
    duration = _units(parameters.get("duration"), "视频时长", allow_fraction=True)
    max_duration = _integer(
        _formula_value(formula, "max_duration_seconds"),
        "最大视频时长",
        minimum=1,
    )
    if duration <= 0 or duration > max_duration:
        raise MeteringValidationError("视频时长超出计量公式范围")
    outputs = _output_count(
        parameters,
        _integer(_formula_value(formula, "max_outputs"), "最大视频数量", minimum=1),
    )
    duration_charge = _checked_multiply(
        _integer(_formula_value(formula, "tokens_per_second"), "每秒视频 token", minimum=1),
        duration,
    )
    subtotal = _checked_add(
        _integer(_formula_value(formula, "base_tokens"), "视频基础 token"),
        duration_charge,
    )
    has_audio = any(
        bool(parameters.get(name, False))
        for name in ("generate_audio", "audio", "sound", "vidu_audio")
    )
    audio_multiplier = (
        _integer(_formula_value(formula, "audio_multiplier"), "视频音频倍率", minimum=1)
        if has_audio
        else 1
    )
    return _checked_multiply(
        subtotal,
        outputs,
        _resolution_multiplier(formula, parameters),
        audio_multiplier,
    )


def evaluate_speech_metering_tokens(
    formula: Any,
    raw_usage: Mapping[str, Any],
) -> int:
    unit = _formula_value(formula, "unit")
    if unit not in {"characters", "provider_tokens", "seconds"}:
        raise MeteringValidationError("语音计量单位不受支持")
    consumed = _units(
        raw_usage.get(unit),
        {"characters": "字符数", "provider_tokens": "供应商 token", "seconds": "音频时长"}[unit],
        allow_fraction=unit == "seconds",
    )
    maximum = _integer(_formula_value(formula, "max_units"), "最大语音计量单位", minimum=1)
    if consumed > maximum:
        raise MeteringValidationError("语音用量超出任务配置快照上限")
    return _checked_multiply(
        consumed,
        _integer(_formula_value(formula, "tokens_per_unit"), "每单位语音 token", minimum=1),
    )


def evaluate_metering_tokens(
    formula: Any,
    *,
    parameters: Mapping[str, Any] | None = None,
    raw_usage: Mapping[str, Any] | None = None,
) -> int:
    kind = _formula_value(formula, "kind")
    accepted_parameters = parameters or {}
    usage = raw_usage or {}
    if kind == "llm":
        return evaluate_llm_metering_tokens(formula, usage)
    if kind == "image":
        return evaluate_image_metering_tokens(formula, accepted_parameters)
    if kind == "video":
        return evaluate_video_metering_tokens(formula, accepted_parameters)
    if kind == "speech":
        return evaluate_speech_metering_tokens(formula, usage)
    raise MeteringValidationError(f"不支持的计量公式类型：{kind}")


def maximum_metering_tokens(
    formula: Any,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> int:
    kind = _formula_value(formula, "kind")
    if kind == "llm":
        return evaluate_llm_metering_tokens(
            formula,
            {
                "input_tokens": _formula_value(formula, "max_input_tokens"),
                "output_tokens": _formula_value(formula, "max_output_tokens"),
            },
        )
    if kind in {"image", "video"}:
        return evaluate_metering_tokens(formula, parameters=parameters)
    if kind == "speech":
        unit = _formula_value(formula, "unit")
        return evaluate_speech_metering_tokens(
            formula,
            {unit: _formula_value(formula, "max_units")},
        )
    raise MeteringValidationError(f"不支持的计量公式类型：{kind}")
