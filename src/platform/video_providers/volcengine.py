from __future__ import annotations

from pydantic import SecretStr

from src.models.ark_seedance import ArkSeedanceVideoModel
from src.models.provider_result import ProviderGenerationResult

from .interface import VideoGenerationRequest


class VolcengineSeedanceProvider:
    """Seedance implementation backed by the locally configured Ark endpoint."""

    provider_name = "volcengine-ark"

    def __init__(self, model_id: str, credential: SecretStr) -> None:
        self.model_id = model_id
        # The logical catalog ID selects this implementation. ArkSeedanceVideoModel
        # resolves the actual Ark endpoint/model from ARK_BASE_URL and
        # ARK_SEEDANCE_MODEL, which are deployment-owned settings.
        self._client = ArkSeedanceVideoModel(
            {"api_key": credential.get_secret_value()}
        )

    def generate(self, request: VideoGenerationRequest) -> ProviderGenerationResult:
        parameters = dict(request.parameters)
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
