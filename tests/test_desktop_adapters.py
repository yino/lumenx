from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import Executor, Future
from datetime import UTC, datetime, timedelta

import pytest

from src.platform.contracts import (
    BillingService,
    CredentialProvider,
    IdentityContextProvider,
    MediaStorage,
    MediaWrite,
    TaskDispatcher,
    UsageSettlement,
    WorkspaceContext,
)
from src.platform.credentials import CredentialResolutionError
from src.platform.desktop_adapters import (
    DesktopCredentialProvider,
    DesktopMediaStorage,
    InProcessTaskDispatcher,
    LocalIdentityContextProvider,
    NoOpDesktopBillingService,
)


class ImmediateExecutor(Executor):
    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)
        return future


@pytest.fixture
def context() -> WorkspaceContext:
    identity = LocalIdentityContextProvider().current_user()
    return WorkspaceContext(identity=identity, workspace_id="desktop-workspace")


def test_desktop_identity_and_credentials_use_local_configuration() -> None:
    identity = LocalIdentityContextProvider()
    credentials = DesktopCredentialProvider({"DASHSCOPE_API_KEY": "local-secret"})

    assert isinstance(identity, IdentityContextProvider)
    assert isinstance(credentials, CredentialProvider)
    assert identity.current_user().user_id == "desktop-local-user"
    assert credentials.resolve("DASHSCOPE_API_KEY").get_secret_value() == "local-secret"
    with pytest.raises(CredentialResolutionError, match="桌面设置"):
        DesktopCredentialProvider({}).resolve("DASHSCOPE_API_KEY")


def test_desktop_media_storage_writes_below_output_and_returns_static_url(
    tmp_path,
    context,
) -> None:
    storage = DesktopMediaStorage(tmp_path / "output")
    content = b"desktop-image"

    stored = storage.store(
        context,
        MediaWrite(
            content=content,
            content_type="image/png",
            filename="../../封面.PNG",
            project_id="project-1",
        ),
    )

    assert isinstance(storage, MediaStorage)
    assert stored.object_key.startswith("media/project-1/")
    assert stored.object_key.endswith(".png")
    assert stored.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert (tmp_path / "output" / stored.object_key).read_bytes() == content
    assert storage.authorized_url(
        context,
        stored.media_id,
        datetime.now(UTC) + timedelta(minutes=5),
    ) == f"/files/{stored.object_key}"
    storage.delete(context, stored.media_id)
    assert not (tmp_path / "output" / stored.object_key).exists()


def test_in_process_dispatcher_executes_task_id_without_external_queue() -> None:
    received: list[str] = []
    dispatcher = InProcessTaskDispatcher(received.append, executor=ImmediateExecutor())

    assert isinstance(dispatcher, TaskDispatcher)
    dispatcher.dispatch("task-1")

    assert received == ["task-1"]


def test_desktop_billing_explicitly_reserves_zero_tickets(context) -> None:
    billing = NoOpDesktopBillingService()
    task_id = str(uuid.uuid4())

    assert isinstance(billing, BillingService)
    reservation = billing.reserve(context, task_id, 999999, 1000)
    assert reservation.quoted_microtickets == 0
    assert reservation.hold_id == f"desktop-no-charge:{task_id}"
    billing.settle(
        context,
        UsageSettlement(task_id=task_id, metering_tokens=999999, raw_usage={}),
    )
    billing.release(context, task_id, "本地任务无需计费")
