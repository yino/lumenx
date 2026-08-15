from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderGenerationResult:
    output_path: str
    elapsed_seconds: float
    raw_usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderTextResult:
    content: str
    raw_usage: Mapping[str, Any]
    provider_request_id: str | None = None
