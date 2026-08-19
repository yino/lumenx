from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .credentials import reject_plaintext_secrets


ALLOWED_LABELS = {
    "action",
    "capability",
    "error_code",
    "operation",
    "outcome",
    "provider",
    "queue",
    "resource",
    "status",
}

_HIGH_CARDINALITY_VALUE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}|[0-9a-f]{24,})$",
    re.IGNORECASE,
)
_RAW_CONTENT_FIELDS = {
    "content",
    "input",
    "messages",
    "output",
    "payload",
    "prompt",
    "provider_payload",
    "raw_provider_usage",
    "request_payload",
    "response_payload",
    "script",
    "text",
    "phone",
    "phone_canonical",
    "reset_credential",
    "object_key",
    "provider_diagnostics",
    "offline_reference",
    "idempotency_key",
}


def _labels_key(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    normalized = tuple(sorted((str(key), str(value)) for key, value in (labels or {}).items()))
    if any(key not in ALLOWED_LABELS for key, _value in normalized):
        raise ValueError("指标标签包含未批准字段")
    if any(len(value) > 80 for _key, value in normalized):
        raise ValueError("指标标签值过长")
    if any(_HIGH_CARDINALITY_VALUE.fullmatch(value) for _key, value in normalized):
        raise ValueError("指标标签不允许使用资源标识")
    return normalized


def _reject_raw_content_fields(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = re.sub(r"[^a-z0-9]+", "_", str(raw_key).lower()).strip("_")
            if key in _RAW_CONTENT_FIELDS:
                raise ValueError(f"{path}.{raw_key} 不允许包含原始用户或供应商内容")
            _reject_raw_content_fields(item, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            _reject_raw_content_fields(item, path=f"{path}[{index}]")


@dataclass(frozen=True, slots=True)
class HistogramSnapshot:
    count: int
    sum: float
    maximum: float


class MetricsRegistry:
    def __init__(self, *, max_series: int = 1000) -> None:
        if max_series < 1:
            raise ValueError("指标序列上限必须为正整数")
        self._lock = threading.RLock()
        self._max_series = max_series
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], HistogramSnapshot
        ] = {}

    @staticmethod
    def _key(name: str, labels: Mapping[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        if not name or not all(character.islower() or character.isdigit() or character in "_:" for character in name):
            raise ValueError("指标名称格式无效")
        return name, _labels_key(labels)

    def increment(self, name: str, value: float = 1, *, labels: Mapping[str, str] | None = None) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError("计数指标增量必须是非负有限数")
        key = self._key(name, labels)
        with self._lock:
            self._reserve_series(key)
            self._counters[key] += value

    def gauge(self, name: str, value: float, *, labels: Mapping[str, str] | None = None) -> None:
        if not math.isfinite(value):
            raise ValueError("仪表指标值必须是有限数")
        key = self._key(name, labels)
        with self._lock:
            self._reserve_series(key)
            self._gauges[key] = value

    def observe(self, name: str, value: float, *, labels: Mapping[str, str] | None = None) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError("直方图观测值必须是非负有限数")
        key = self._key(name, labels)
        with self._lock:
            self._reserve_series(key)
            current = self._histograms.get(key)
            self._histograms[key] = HistogramSnapshot(
                count=(current.count if current else 0) + 1,
                sum=(current.sum if current else 0) + value,
                maximum=max(current.maximum if current else 0, value),
            )

    def _reserve_series(self, key: tuple[str, tuple[tuple[str, str], ...]]) -> None:
        existing = key in self._counters or key in self._gauges or key in self._histograms
        if existing:
            return
        series_count = len(self._counters) + len(self._gauges) + len(self._histograms)
        if series_count >= self._max_series:
            raise ValueError("指标序列数量超过安全上限")

    def snapshot(self) -> dict[str, Any]:
        def rows(values):
            return [
                {"name": name, "labels": dict(labels), "value": value}
                for (name, labels), value in sorted(values.items())
            ]

        with self._lock:
            histograms = []
            for (name, labels), value in sorted(self._histograms.items()):
                histograms.append(
                    {
                        "name": name,
                        "labels": dict(labels),
                        "count": value.count,
                        "sum": value.sum,
                        "maximum": value.maximum,
                    }
                )
            return {
                "generated_at": time.time(),
                "counters": rows(self._counters),
                "gauges": rows(self._gauges),
                "histograms": histograms,
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


class StructuredEventLogger:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("lumenx.events")

    def emit(self, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
        if not event.strip() or len(event) > 120:
            raise ValueError("结构化事件名称无效")
        reject_plaintext_secrets(fields, path="结构化事件")
        _reject_raw_content_fields(fields, path="结构化事件")
        encoded = json.dumps(
            {"event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("结构化事件过大")
        self.logger.log(level, encoded)


metrics = MetricsRegistry()
events = StructuredEventLogger()
