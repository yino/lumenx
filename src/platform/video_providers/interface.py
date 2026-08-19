from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.models.provider_result import ProviderGenerationResult


ProviderSubmissionCallback = Callable[[str, str | None, str | None], None]


@dataclass(frozen=True, slots=True)
class VideoGenerationRequest:
    """Provider-neutral input for one video generation attempt."""

    model_id: str
    prompt: str
    output_path: str
    mode: str
    input_urls: tuple[str, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=dict)
    on_provider_submission: ProviderSubmissionCallback | None = None


class VideoProvider(Protocol):
    """Stable worker boundary implemented once per video provider family."""

    provider_name: str
    model_id: str

    def generate(self, request: VideoGenerationRequest) -> ProviderGenerationResult: ...


class VideoProviderFactory(Protocol):
    def create(self, model_id: str) -> VideoProvider: ...
