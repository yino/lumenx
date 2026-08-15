from abc import ABC, abstractmethod
from typing import Dict, Any, Tuple

from .provider_result import ProviderGenerationResult

class VideoGenModel(ABC):
    """Abstract base class for video generation models."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config

    @abstractmethod
    def generate(self, prompt: str, output_path: str, **kwargs) -> Tuple[str, float]:
        """
        Generates a video from a prompt.
        
        Args:
            prompt: The input text prompt.
            output_path: The path to save the generated video.
            **kwargs: Additional arguments.
            
        Returns:
            A tuple containing:
            - The path to the generated video file.
            - The duration of the API generation process in seconds (excluding download).
        """
        pass

    def generate_with_usage(
        self,
        prompt: str,
        output_path: str,
        **kwargs,
    ) -> ProviderGenerationResult:
        generated_path, elapsed_seconds = self.generate(
            prompt,
            output_path,
            **kwargs,
        )
        raw_usage = {
            "audio": any(
                bool(kwargs.get(name, False))
                for name in ("generate_audio", "audio", "sound", "vidu_audio")
            ),
            "duration_seconds": kwargs.get("duration"),
            "output_count": kwargs.get("output_count", kwargs.get("count", 1)),
            "resolution": kwargs.get("resolution", kwargs.get("size")),
        }
        return ProviderGenerationResult(
            output_path=generated_path,
            elapsed_seconds=float(elapsed_seconds),
            raw_usage=raw_usage,
        )
