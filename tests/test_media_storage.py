from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.platform.content_repositories import ScopedDocumentNotFoundError
from src.platform.content_service import CloudContentService
from src.platform.contracts import MediaStorage, MediaWrite, WorkspaceContext
from src.platform.db_models import MediaObjectRecord, WorkspaceRecord
from src.platform.media_storage import (
    CloudMediaStorage,
    MediaStorageError,
    MediaValidationError,
)
from tests.test_content_repositories import RepositoryDatabase, _create_scope
from tests.test_content_service import FakeScriptProcessor


class FakePrivateObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.signed: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.fail_put = False

    def put(self, object_key: str, content: bytes, content_type: str) -> None:
        if self.fail_put:
            raise RuntimeError("upload failed")
        self.objects[object_key] = (content, content_type)

    def signed_get_url(self, object_key: str, expires_seconds: int) -> str:
        self.signed.append((object_key, expires_seconds))
        return f"https://private.example/{object_key}?expires={expires_seconds}"

    def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)
        self.objects.pop(object_key, None)


@pytest.fixture
def media_environment():
    database = RepositoryDatabase()
    context = _create_scope(database)
    content = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    )
    project = content.create_project(
        context,
        title="第一集",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
    )
    object_store = FakePrivateObjectStore()
    storage = CloudMediaStorage(database, object_store)
    yield database, context, project.document.id, object_store, storage
    database.engine.dispose()


def test_cloud_media_store_derives_private_key_and_persists_metadata(
    media_environment,
) -> None:
    database, context, project_id, object_store, storage = media_environment
    content = b"image-content"

    stored = storage.store(
        context,
        MediaWrite(
            content=content,
            content_type="image/png",
            filename="../../private/avatar.PNG",
            project_id=project_id,
            provenance={
                "origin": "upload",
                "workspace_id": "forged-workspace",
            },
        ),
    )

    expected_prefix = (
        f"users/{context.identity.user_id}/workspaces/{context.workspace_id}/"
        f"projects/{project_id}/"
    )
    assert stored.object_key.startswith(expected_prefix)
    assert stored.object_key.endswith(".png")
    assert "private" not in stored.object_key
    assert object_store.objects[stored.object_key] == (content, "image/png")
    assert stored.size_bytes == len(content)
    assert stored.checksum_sha256 == hashlib.sha256(content).hexdigest()
    assert isinstance(storage, MediaStorage)

    with database.session_factory() as session:
        record = session.get(MediaObjectRecord, int(stored.media_id))
        assert record is not None
        assert record.lifecycle_state == "active"
        assert record.mime_type == "image/png"
        assert record.project_id == int(project_id)
        assert record.provenance["workspace_id"] == context.workspace_id
        assert record.provenance["project_id"] == project_id
        assert record.provenance["filename"] == "avatar.PNG"


def test_cloud_media_store_marks_failed_upload_for_reconciliation(
    media_environment,
) -> None:
    database, context, project_id, object_store, storage = media_environment
    object_store.fail_put = True

    with pytest.raises(MediaStorageError, match="上传失败"):
        storage.store(
            context,
            MediaWrite(
                content=b"image-content",
                content_type="image/png",
                filename="avatar.png",
                project_id=project_id,
            ),
        )

    with database.session_factory() as session:
        records = list(session.scalars(select(MediaObjectRecord)))
        assert len(records) == 1
        assert records[0].lifecycle_state == "failed"


def test_media_access_isolated_by_user_and_workspace(media_environment) -> None:
    database, context, project_id, object_store, storage = media_environment
    stored = storage.store(
        context,
        MediaWrite(
            content=b"image-content",
            content_type="image/png",
            filename="avatar.png",
            project_id=project_id,
        ),
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    url = storage.authorized_url(context, stored.media_id, expires_at)
    assert url.startswith("https://private.example/")
    assert object_store.signed[-1][0] == stored.object_key

    foreign_context = _create_scope(database)
    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        storage.authorized_url(foreign_context, stored.media_id, expires_at)

    with database.session_factory.begin() as session:
        other_workspace = WorkspaceRecord(
            user_id=int(context.identity.user_id),
            name="其他工作区",
        )
        session.add(other_workspace)
        session.flush()
        other_workspace_id = other_workspace.id
    other_workspace_context = WorkspaceContext(
        identity=context.identity,
        workspace_id=str(other_workspace_id),
    )
    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        storage.authorized_url(
            other_workspace_context,
            stored.media_id,
            expires_at,
        )


def test_media_store_rejects_foreign_project_before_upload(media_environment) -> None:
    database, _context, _project_id, object_store, storage = media_environment
    foreign_context = _create_scope(database)
    foreign_content = CloudContentService(
        database,
        script_processor=FakeScriptProcessor(),
    )
    foreign_project = foreign_content.create_project(
        foreign_context,
        title="其他项目",
        text="正文",
        skip_analysis=True,
        workflow_mode="r2v",
    )
    owner_context = _create_scope(database)

    with pytest.raises(ScopedDocumentNotFoundError, match="资源不存在"):
        storage.store(
            owner_context,
            MediaWrite(
                content=b"image-content",
                content_type="image/png",
                filename="avatar.png",
                project_id=foreign_project.document.id,
            ),
        )
    assert object_store.objects == {}


def test_media_signed_url_bounds_and_soft_delete(media_environment) -> None:
    database, context, _project_id, object_store, storage = media_environment
    stored = storage.store(
        context,
        MediaWrite(
            content=b"shared-audio",
            content_type="audio/mpeg",
            filename="voice.mp3",
            provenance={"origin": "voice_clone_source"},
        ),
    )
    assert "projects/shared/upload-" in stored.object_key
    assert stored.object_key.endswith(".mp3")

    with pytest.raises(MediaValidationError, match="15 分钟"):
        storage.authorized_url(
            context,
            stored.media_id,
            datetime.now(UTC) + timedelta(minutes=16),
        )
    with pytest.raises(MediaValidationError, match="包含时区"):
        storage.authorized_url(
            context,
            stored.media_id,
            datetime.now() + timedelta(minutes=5),
        )

    storage.delete(context, stored.media_id)
    with database.session_factory() as session:
        record = session.get(MediaObjectRecord, int(stored.media_id))
        assert record is not None
        assert record.lifecycle_state == "deleted"
        assert record.deleted_at is not None
        assert record.retention_expires_at is not None
    assert object_store.deleted == []


@pytest.mark.parametrize(
    ("content", "content_type", "filename"),
    [
        (b"", "image/png", "image.png"),
        (b"image", "invalid", "image.png"),
        (b"image", "image/png", ""),
    ],
)
def test_media_payload_validation(
    media_environment,
    content: bytes,
    content_type: str,
    filename: str,
) -> None:
    _database, context, _project_id, object_store, storage = media_environment
    with pytest.raises(MediaValidationError):
        storage.store(
            context,
            MediaWrite(
                content=content,
                content_type=content_type,
                filename=filename,
            ),
        )
    assert object_store.objects == {}
