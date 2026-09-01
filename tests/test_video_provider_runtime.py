from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from src.models.provider_result import ProviderGenerationResult
from src.platform.contracts import ModelRouteSnapshot
from src.platform.model_routing import RequestScopedModelClientFactory
from src.platform.provider_runtime import ProductionProviderInvoker
from src.platform.settings import DeploymentSettings
from src.platform.video_providers import (
    ModelIdVideoProviderFactory,
    VideoGenerationRequest,
)
from src.platform.video_providers.aliyun import AliyunWanVideoProvider
from src.platform.video_providers.volcengine import VolcengineSeedanceProvider
from src.platform.video_providers.xlinks import XlinksGrokVideoProvider


class RecordingCredentials:
    def __init__(self) -> None:
        self.references: list[str] = []

    def resolve(self, secret_ref: str) -> SecretStr:
        self.references.append(secret_ref)
        return SecretStr(f"secret-for-{secret_ref.lower()}")


def _production_settings(tmp_path: Path) -> DeploymentSettings:
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    return DeploymentSettings(
        _env_file=None,
        deployment_mode="cloud",
        database_url="postgresql+psycopg://lumenx:test@postgres/lumenx",
        redis_url="redis://redis:6379/0",
        oss_endpoint="oss.example.invalid",
        oss_bucket_name="private",
        oss_access_key_id="id",
        oss_access_key_secret="secret",
        session_secret="s" * 32,
        model_catalog_path=catalog,
        provider_secret_refs="DASHSCOPE_API_KEY,ARK_API_KEY",
        provider_output_root=tmp_path / "provider-results",
    )


def test_model_id_selects_vendor_and_seedance_reads_local_ark_model(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/plan/v3"
    )
    monkeypatch.setenv("ARK_SEEDANCE_MODEL", "ep-local-seedance-2")
    credentials = RecordingCredentials()
    factory = ModelIdVideoProviderFactory(credentials)

    seedance = factory.create("seedance-2.0-r2v")
    wan = factory.create("wan2.7-i2v")
    grok = factory.create("grok-imagine-video", provider="xlinks")
    gemini = factory.create("gemini-omni-1.1-flash", provider="xlinks")

    assert isinstance(seedance, VolcengineSeedanceProvider)
    assert seedance.model_id == "seedance-2.0-r2v"
    assert seedance._client.model_name == "ep-local-seedance-2"
    assert isinstance(wan, AliyunWanVideoProvider)
    assert wan.model_id == "wan2.7-i2v"
    assert isinstance(grok, XlinksGrokVideoProvider)
    assert grok.model_id == "grok-imagine-video"
    assert isinstance(gemini, XlinksGrokVideoProvider)
    assert gemini.model_id == "gemini-omni-1.1-flash"
    assert credentials.references == [
        "ARK_API_KEY",
        "DASHSCOPE_API_KEY",
        "XLINKS_API_KEY",
        "XLINKS_API_KEY",
    ]


def test_request_scoped_client_delegates_video_selection_by_model_id() -> None:
    credentials = RecordingCredentials()

    class VideoProviders:
        def __init__(self) -> None:
            self.model_ids: list[str] = []

        def create(self, model_id: str, *, provider: str | None = None):
            self.model_ids.append(model_id)
            return SimpleNamespace(model_id=model_id)

    video_providers = VideoProviders()
    snapshot = ModelRouteSnapshot(
        config_version_id="5",
        route_id="route-seedance",
        capability="video.r2v",
        provider="ark",
        provider_model_id="seedance-2.0-r2v",
        display_name="Seedance 2.0 R2V（本地）",
        parameters={"duration": 5},
        metering_formula={"kind": "video"},
        fallback_policy={"enabled": False},
        secret_ref="ARK_API_KEY",
    )

    client = RequestScopedModelClientFactory(
        credentials,
        video_providers=video_providers,
    ).create(snapshot)

    assert client.adapter.model_id == "seedance-2.0-r2v"
    assert video_providers.model_ids == ["seedance-2.0-r2v"]
    assert credentials.references == []


def test_worker_video_runtime_passes_only_typed_model_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    captured: dict[str, VideoGenerationRequest] = {}

    class Adapter:
        def generate(self, request: VideoGenerationRequest):
            captured["request"] = request
            Path(request.output_path).write_bytes(b"video")
            request.on_provider_submission("volcengine-ark", "task-1", "request-1")
            return ProviderGenerationResult(
                output_path=request.output_path,
                elapsed_seconds=0.1,
                raw_usage={"duration_seconds": 5, "resolution": "1080p"},
            )

    class Client:
        adapter = Adapter()

        @staticmethod
        def execution_parameters():
            return {
                "model": "seedance-2.0-r2v",
                "duration": 5,
                "resolution": "1080p",
            }

    class Submission:
        recorded = False

        def __call__(self, *_args):
            self.recorded = True

    task = SimpleNamespace(
        task_id="31",
        attempt_id="32",
        capability="video.r2v",
        request_payload={"content": {"prompt": "角色走向镜头"}},
        model_route=SimpleNamespace(
            provider="ark",
            provider_model_id="seedance-2.0-r2v",
        ),
        provider_inputs=(
            SimpleNamespace(signed_url="https://example.com/ref-1.png"),
            SimpleNamespace(signed_url="https://example.com/ref-2.png"),
        ),
    )

    outcome = ProductionProviderInvoker(_production_settings(tmp_path)).invoke(
        Client(), task, Submission()
    )

    request = captured["request"]
    assert request.model_id == "seedance-2.0-r2v"
    assert request.mode == "r2v"
    assert request.input_urls == (
        "https://example.com/ref-1.png",
        "https://example.com/ref-2.png",
    )
    assert request.parameters == {"duration": 5, "resolution": "1080p"}
    assert outcome.raw_usage["duration_seconds"] == 5
    assert outcome.result["outputs"][0]["content_type"] == "video/mp4"
