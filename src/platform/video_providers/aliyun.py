from __future__ import annotations

from pydantic import SecretStr

from src.models.provider_result import ProviderGenerationResult
from src.models.wanx import WanxModel

from .interface import VideoGenerationRequest


class AliyunWanVideoProvider:
    """Wan video implementation backed by Alibaba Cloud DashScope."""

    provider_name = "dashscope"

    def __init__(self, model_id: str, credential: SecretStr) -> None:
        self.model_id = model_id
        self._client = WanxModel(
            {
                "api_key": credential.get_secret_value(),
                "params": {"model_name": model_id},
            }
        )

    def generate(self, request: VideoGenerationRequest) -> ProviderGenerationResult:
        parameters = dict(request.parameters)
        parameters["model"] = request.model_id
        parameters["generation_mode"] = request.mode
        if request.mode == "r2v":
            parameters["ref_image_urls"] = list(request.input_urls)
        elif request.input_urls:
            parameters["img_url"] = request.input_urls[0]
        if request.on_provider_submission is not None:
            parameters["on_provider_ids"] = request.on_provider_submission
        return self._client.generate_with_usage(
            request.prompt,
            request.output_path,
            **parameters,
        )
