from __future__ import annotations

from src.apps.playground.models import PlaygroundGeneration, PlaygroundMode
from src.apps.playground.service import PlaygroundService


class MemoryStorage:
    def __init__(self, generation: PlaygroundGeneration) -> None:
        self.generation = generation
        self.updates = []

    def update_generation(self, generation: PlaygroundGeneration) -> None:
        self.generation = generation
        self.updates.append(generation.model_copy(deep=True))


class ProviderAwareImageModel:
    def generate(self, prompt: str, output_path: str, **kwargs):
        kwargs["on_provider_ids"]("dashscope", "remote-task-1", "request-1")
        return output_path, 0.1


class ProviderAwareArkModel:
    def generate(self, prompt: str, output_path: str, **kwargs):
        kwargs["on_provider_ids"]("volcengine-ark", "cgt-video-1", "request-video-1")
        return output_path, 0.1


def test_playground_persists_remote_image_task_before_polling() -> None:
    generation = PlaygroundGeneration(
        id="generation-1",
        mode=PlaygroundMode.T2I,
        model_id="wan2.7-image-pro",
        prompt="雨夜巷战",
        created_at="2026-08-20T00:00:00+00:00",
    )
    storage = MemoryStorage(generation)
    service = PlaygroundService(storage)
    service._wanx_image_model = ProviderAwareImageModel()

    service._generate_image_wanx(generation, "output/result.png", 0)

    assert storage.updates
    persisted = storage.updates[-1]
    assert persisted.provider_name == "dashscope"
    assert persisted.provider_task_id == "remote-task-1"
    assert persisted.provider_request_id == "request-1"


def test_playground_persists_remote_ark_video_task_before_polling() -> None:
    generation = PlaygroundGeneration(
        id="generation-video-1",
        mode=PlaygroundMode.T2V,
        model_id="seedance-2.0-t2v",
        prompt="雨巷交锋",
        created_at="2026-08-21T00:00:00+00:00",
    )
    storage = MemoryStorage(generation)
    service = PlaygroundService(storage)
    service._ark_seedance_video_model = ProviderAwareArkModel()

    service._generate_video_ark(generation, "output/result.mp4")

    persisted = storage.updates[-1]
    assert persisted.provider_name == "volcengine-ark"
    assert persisted.provider_task_id == "cgt-video-1"
    assert persisted.provider_request_id == "request-video-1"


def test_playground_backfills_dashscope_task_id_from_legacy_network_error() -> None:
    generation = PlaygroundGeneration(
        id="legacy-generation",
        mode=PlaygroundMode.T2V,
        model_id="happyhorse-1.1-t2v",
        prompt="雨夜街道",
        status="failed",
        error=(
            "All 1 batch items failed: HTTPSConnectionPool(host='dashscope.aliyuncs.com', "
            "port=443): Max retries exceeded with url: "
            "/api/v1/tasks/legacy-task-123"
        ),
        created_at="2026-08-21T00:00:00+00:00",
    )
    storage = MemoryStorage(generation)
    service = PlaygroundService(storage)

    hydrated = service.hydrate_provider_metadata(generation)

    assert hydrated.provider_name == "dashscope"
    assert hydrated.provider_task_id == "legacy-task-123"
    assert storage.updates[-1].provider_task_id == "legacy-task-123"
