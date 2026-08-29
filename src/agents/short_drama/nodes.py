"""Typed wrappers for existing script/polish functions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.errors import AgentError

T = TypeVar("T", bound=BaseModel)


def parse_structured_output(value: Any, schema: type[T]) -> T:
    """Validate an LLM result before it can enter the graph state."""
    if isinstance(value, schema):
        return value
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError as exc:
            raise AgentError("LLM 输出不是有效 JSON", code="LLM_OUTPUT_INVALID") from exc
    try:
        return schema.model_validate(value)
    except ValidationError as exc:
        raise AgentError("LLM 输出未通过结构化校验", code="LLM_OUTPUT_SCHEMA_INVALID") from exc


def run_with_retries(
    call: Callable[[], Any],
    schema: type[T],
    *,
    retries: int = 2,
    fallback: Callable[[], Any] | None = None,
) -> T:
    last_error: Exception | None = None
    for _ in range(max(0, retries) + 1):
        try:
            return parse_structured_output(call(), schema)
        except Exception as exc:  # validation and provider wrappers are retried uniformly
            last_error = exc
    if fallback is not None:
        return parse_structured_output(fallback(), schema)
    raise AgentError(
        "LLM 结构化输出多次校验失败",
        code="LLM_OUTPUT_RETRIES_EXHAUSTED",
        retryable=False,
    ) from last_error


def wrap_script_analysis(analyzer: Callable[[str], Any], text: str, schema: type[T]) -> T:
    return run_with_retries(lambda: analyzer(text), schema)


def wrap_prompt_polish(polisher: Callable[[Mapping[str, Any]], Any], payload: Mapping[str, Any], schema: type[T]) -> T:
    return run_with_retries(lambda: polisher(dict(payload)), schema)
