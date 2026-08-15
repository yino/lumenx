from __future__ import annotations

from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from pydantic import SecretStr

from src.apps.comic_gen.models import Script
from src.platform.content_repositories import PostgresProjectRepository
from src.platform.contracts import MediaWrite, WorkspaceContext
from src.platform.desktop_adapters import (
    DesktopCredentialProvider,
    DesktopMediaStorage,
    InProcessTaskDispatcher,
    NoOpDesktopBillingService,
)
from src.platform.desktop_repositories import DesktopProjectRepository
from src.platform.media_storage import CloudMediaStorage
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_media_storage import FakePrivateObjectStore


def _project(project_id: str) -> Script:
    return Script(
        id=project_id,
        title="合同测试项目",
        original_text="正文",
        created_at=1.0,
        updated_at=1.0,
    )


@pytest.mark.parametrize("repository_kind", ["desktop", "cloud"])
def test_project_repository_contract(repository_kind: str, tmp_path) -> None:
    if repository_kind == "desktop":
        repository = DesktopProjectRepository(tmp_path / "projects.json")
        context = WorkspaceContext(identity=_create_scope(RepositoryDatabase()).identity, workspace_id="local")
        database = None
    else:
        database = RepositoryDatabase()
        context = _create_scope(database)
        repository = PostgresProjectRepository(database)
    project = _project(str(uuid.uuid4()))

    stored = repository.add(context, project)
    resource_id = stored.document.id
    loaded = repository.get(context, resource_id)
    assert loaded is not None
    assert loaded.document == stored.document
    assert [item.document.id for item in repository.list(context)] == [resource_id]
    repository.soft_delete(context, resource_id)
    assert repository.get(context, resource_id) is None
    if database is not None:
        database.engine.dispose()


@pytest.mark.parametrize("storage_kind", ["desktop", "cloud"])
def test_media_storage_contract(storage_kind: str, tmp_path) -> None:
    database = RepositoryDatabase()
    context = _create_scope(database)
    storage = (
        DesktopMediaStorage(tmp_path / "output")
        if storage_kind == "desktop"
        else CloudMediaStorage(database, FakePrivateObjectStore())
    )
    stored = storage.store(
        context,
        MediaWrite(content=b"image", content_type="image/png", filename="image.png"),
    )
    url = storage.authorized_url(context, stored.media_id, datetime.now(UTC) + timedelta(minutes=1))
    assert isinstance(url, str) and url
    storage.delete(context, stored.media_id)
    database.engine.dispose()


def test_desktop_credential_task_and_billing_contracts() -> None:
    credential = DesktopCredentialProvider({"DASHSCOPE_API_KEY": "local-secret"})
    assert credential.resolve("DASHSCOPE_API_KEY") == SecretStr("local-secret")
    completed = Future()
    dispatcher = InProcessTaskDispatcher(lambda task_id: completed.set_result(task_id))
    dispatcher.dispatch("task-1")
    assert completed.result(timeout=2) == "task-1"
    dispatcher.close()
    billing = NoOpDesktopBillingService()
    context = WorkspaceContext(identity=_create_scope(RepositoryDatabase()).identity, workspace_id="local")
    reservation = billing.reserve(context, "task-1", 1000, 1000)
    assert reservation.quoted_microtickets == 0
