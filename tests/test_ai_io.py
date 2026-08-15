from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.platform.ai_io import (
    AIOutputFinalizationService,
    AIProviderInputResolver,
    DownloadedProviderOutput,
    HTTPProviderOutputDownloader,
    ProviderInputNotFoundError,
    ProviderOutputValidationError,
)
from src.platform.ai_task_state import AITaskStateService
from src.platform.ai_worker import ProviderInvocationOutcome
from src.platform.db_models import (
    AITaskAttemptRecord,
    AITaskRecord,
    MediaObjectRecord,
    TicketHoldRecord,
    UsageEventRecord,
)
from src.platform.media_storage import CloudMediaStorage
from src.platform.ticket_settlement import TicketSettlementService
from tests.test_ai_gateway import RecordingDispatcher, _gateway, _gateway_database, _payload
from tests.test_ai_worker import RecordingClientFactory, _worker
from tests.test_content_repositories import _create_scope
from tests.test_media_storage import FakePrivateObjectStore


PNG_CONTENT = b"\x89PNG\r\n\x1a\n" + b"test-image"


class OutputInvoker:
    def __init__(self, result=None) -> None:
        self.result = result if result is not None else {
            "outputs": [
                {
                    "url": "https://cdn.aliyuncs.com/generated/image.png",
                    "filename": "generated.png",
                    "content_type": "image/png",
                }
            ]
        }
        self.calls = []

    def invoke(self, client, task, on_provider_submission):
        self.calls.append(task)
        on_provider_submission("dashscope", "provider-task-io", "request-io")
        return ProviderInvocationOutcome(
            raw_usage={"image_count": 1},
            result=self.result,
        )


class FakeOutputDownloader:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.references = []

    def download(self, provider, reference):
        self.references.append((provider, reference))
        if self.error is not None:
            raise self.error
        return DownloadedProviderOutput(
            content=PNG_CONTENT,
            content_type="image/png",
            filename=reference.filename,
        )


def _submitted_output_task(database, context, media_id, asset_id, result=None):
    submitted = _gateway(database, RecordingDispatcher()).submit(
        context,
        _payload(media_id, asset_id),
    )
    worker = _worker(
        database,
        RecordingClientFactory(),
        OutputInvoker(result),
    )
    assert worker.execute(submitted.task_id).status == "provider_succeeded"
    return submitted


def _finalizer(database, storage, downloader):
    return AIOutputFinalizationService(
        task_state=AITaskStateService(database),
        settlement=TicketSettlementService(database),
        media_storage=storage,
        downloader=downloader,
    )


def test_provider_inputs_resolve_owned_media_to_short_lived_signed_urls() -> None:
    database, context, media_id, asset_id = _gateway_database()
    object_store = FakePrivateObjectStore()
    storage = CloudMediaStorage(database, object_store)
    resolver = AIProviderInputResolver(storage)
    try:
        resolved = resolver.resolve(
            context,
            request_payload={
                "input_media_ids": [str(media_id)],
                "resource_ids": {"asset": [str(asset_id)]},
            },
            project_id=None,
        )

        assert len(resolved) == 1
        assert resolved[0].media_id == str(media_id)
        assert resolved[0].content_type == "image/png"
        assert resolved[0].signed_url.startswith("https://private.example/")

        foreign_context = _create_scope(database)
        with pytest.raises(ProviderInputNotFoundError, match="输入资源不存在"):
            resolver.resolve(
                foreign_context,
                request_payload={"input_media_ids": [str(media_id)]},
                project_id=None,
            )
    finally:
        database.engine.dispose()


def test_output_finalization_stores_private_media_and_settles_once() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _submitted_output_task(database, context, media_id, asset_id)
    object_store = FakePrivateObjectStore()
    storage = CloudMediaStorage(database, object_store)
    downloader = FakeOutputDownloader()
    finalizer = _finalizer(database, storage, downloader)
    try:
        finalized = finalizer.finalize(context, submitted.task_id)
        repeated = finalizer.finalize(context, submitted.task_id)

        assert finalized.status == "succeeded"
        assert finalized.charged_microtickets == 110_000
        assert len(finalized.media_ids) == 1
        assert repeated.reused is True
        assert repeated.media_ids == finalized.media_ids
        assert len(object_store.objects) == 1
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(submitted.task_id))
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            media = session.get(MediaObjectRecord, int(finalized.media_ids[0]))
            hold = session.scalar(
                select(TicketHoldRecord).where(TicketHoldRecord.task_id == task.id)
            )
            assert task is not None
            assert attempt is not None
            assert media is not None
            assert hold is not None
            assert task.status == "succeeded"
            assert task.result == {
                "media_ids": [finalized.media_ids[0]],
                "output_count": 1,
            }
            assert "url" not in str(task.result)
            assert attempt.status == "succeeded"
            assert hold.status == "settled"
            assert media.provenance["task_id"] == submitted.task_id
            assert media.provenance["attempt_id"] == submitted.attempt_id
            assert session.scalar(select(func.count()).select_from(UsageEventRecord)) == 1
    finally:
        database.engine.dispose()


def test_worker_resolves_inputs_and_recovers_finalization_without_resubmission() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _gateway(database, RecordingDispatcher()).submit(
        context,
        _payload(media_id, asset_id),
    )
    object_store = FakePrivateObjectStore()
    storage = CloudMediaStorage(database, object_store)
    finalizer = _finalizer(database, storage, FakeOutputDownloader())

    class FlakyFinalizer:
        def __init__(self) -> None:
            self.calls = 0

        def finalize(self, worker_context, task_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("worker stopped before finalization")
            return finalizer.finalize(worker_context, task_id)

    flaky = FlakyFinalizer()
    invoker = OutputInvoker()
    worker = _worker(
        database,
        RecordingClientFactory(),
        invoker,
        input_resolver=AIProviderInputResolver(storage),
        output_finalizer=flaky,
    )
    try:
        with pytest.raises(RuntimeError, match="stopped before finalization"):
            worker.execute(submitted.task_id)

        recovered = worker.execute(submitted.task_id)

        assert recovered.status == "succeeded"
        assert recovered.acquired is False
        assert len(invoker.calls) == 1
        assert [item.media_id for item in invoker.calls[0].provider_inputs] == [
            str(media_id)
        ]
        assert flaky.calls == 2
    finally:
        database.engine.dispose()


def test_output_failure_after_provider_billing_settles_and_enters_review() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _submitted_output_task(database, context, media_id, asset_id)
    object_store = FakePrivateObjectStore()
    storage = CloudMediaStorage(database, object_store)
    secret = "sk-provider-output-secret"
    downloader = FakeOutputDownloader(
        error=ProviderOutputValidationError(f"Bearer {secret}"),
    )
    try:
        finalized = _finalizer(database, storage, downloader).finalize(
            context,
            submitted.task_id,
        )

        assert finalized.status == "support_review"
        assert finalized.support_review is True
        assert finalized.charged_microtickets == 110_000
        with database.session_factory() as session:
            task = session.get(AITaskRecord, int(submitted.task_id))
            attempt = session.get(AITaskAttemptRecord, int(submitted.attempt_id))
            hold = session.scalar(
                select(TicketHoldRecord).where(TicketHoldRecord.task_id == task.id)
            )
            assert task is not None
            assert attempt is not None
            assert hold is not None
            assert task.status == "support_review"
            assert task.safe_error_code == "AI_OUTPUT_PROCESSING_FAILED"
            assert task.safe_error_message == "AI 结果处理失败，已进入人工复核"
            assert secret not in str(task.safe_error_message)
            assert task.result is None
            assert attempt.status == "failed"
            assert hold.status == "settled"
    finally:
        database.engine.dispose()


def test_missing_provider_media_output_is_billed_and_sent_to_review() -> None:
    database, context, media_id, asset_id = _gateway_database()
    submitted = _submitted_output_task(
        database,
        context,
        media_id,
        asset_id,
        result={"provider_stage": "completed"},
    )
    storage = CloudMediaStorage(database, FakePrivateObjectStore())
    try:
        finalized = _finalizer(
            database,
            storage,
            FakeOutputDownloader(),
        ).finalize(context, submitted.task_id)

        assert finalized.status == "support_review"
        assert finalized.charged_microtickets == 110_000
        with database.session_factory() as session:
            hold = session.scalar(
                select(TicketHoldRecord).where(
                    TicketHoldRecord.task_id == int(submitted.task_id)
                )
            )
            assert hold is not None
            assert hold.status == "settled"
    finally:
        database.engine.dispose()


def test_http_output_downloader_enforces_provider_host_and_redirect(monkeypatch) -> None:
    class Response:
        url = "https://cdn.aliyuncs.com/generated/image.png"
        headers = {"content-type": "image/png", "content-length": str(len(PNG_CONTENT))}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield PNG_CONTENT

    monkeypatch.setattr("src.platform.ai_io.requests.get", lambda *args, **kwargs: Response())
    downloader = HTTPProviderOutputDownloader(
        {"dashscope": ["aliyuncs.com"]},
        maximum_bytes=1024,
    )
    reference = SimpleOutputReference(
        url="https://cdn.aliyuncs.com/generated/image.png",
        filename="image.png",
    )

    downloaded = downloader.download("dashscope", reference)

    assert downloaded.content == PNG_CONTENT
    with pytest.raises(ProviderOutputValidationError, match="地址"):
        downloader.download(
            "dashscope",
            SimpleOutputReference(
                url="https://127.0.0.1/internal",
                filename="bad.png",
            ),
        )


class SimpleOutputReference:
    def __init__(self, *, url: str, filename: str) -> None:
        self.url = url
        self.filename = filename
        self.declared_content_type = None
