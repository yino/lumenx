from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Generic, Mapping, TypeVar

from pydantic import BaseModel

from src.apps.comic_gen.models import GlobalAssetLibrary, Script, Series

from .contracts import VersionedDocument, WorkspaceContext


DocumentT = TypeVar("DocumentT", bound=BaseModel)


class DesktopJsonRepositoryError(RuntimeError):
    pass


class DesktopJsonDocumentRepository(Generic[DocumentT]):
    """Desktop JSON adapter preserving the legacy ``{id: document}`` format."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | os.PathLike[str], document_type: type[DocumentT]) -> None:
        self.path = Path(path)
        self.document_type = document_type
        self._lock = threading.RLock()
        self._versions: dict[str, int] = {}

    def set_path(self, path: str | os.PathLike[str]) -> None:
        with self._lock:
            next_path = Path(path)
            if next_path != self.path:
                self.path = next_path
                self._versions.clear()

    def load_all(self) -> dict[str, DocumentT]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("桌面文档文件必须是对象")
                documents = {
                    str(resource_id): self.document_type.model_validate(payload)
                    for resource_id, payload in raw.items()
                }
            except Exception as exc:
                raise DesktopJsonRepositoryError(
                    f"无法读取桌面文档文件：{self.path}"
                ) from exc
            self._versions = {
                resource_id: self._versions.get(resource_id, 1)
                for resource_id in documents
            }
            return documents

    def replace_all(self, documents: Mapping[str, DocumentT]) -> None:
        payload = {
            resource_id: document.model_dump(mode="json")
            for resource_id, document in documents.items()
        }
        with self._lock:
            self._write_json(payload)
            self._versions = {
                resource_id: self._versions.get(resource_id, 1)
                for resource_id in documents
            }

    def list(self, context: WorkspaceContext) -> list[VersionedDocument[DocumentT]]:
        del context
        documents = self.load_all()
        return [self._versioned(resource_id, document) for resource_id, document in documents.items()]

    def get(
        self,
        context: WorkspaceContext,
        resource_id: str,
    ) -> VersionedDocument[DocumentT] | None:
        del context
        document = self.load_all().get(resource_id)
        return self._versioned(resource_id, document) if document is not None else None

    def add(
        self,
        context: WorkspaceContext,
        document: DocumentT,
    ) -> VersionedDocument[DocumentT]:
        del context
        resource_id = self._document_id(document)
        with self._lock:
            documents = self.load_all()
            if resource_id in documents:
                raise DesktopJsonRepositoryError("桌面资源已存在")
            documents[resource_id] = document
            self._versions[resource_id] = 1
            self.replace_all(documents)
            return self._versioned(resource_id, document)

    def update(
        self,
        context: WorkspaceContext,
        resource_id: str,
        document: DocumentT,
        expected_version: int,
    ) -> VersionedDocument[DocumentT]:
        del context
        if self._document_id(document) != resource_id:
            raise DesktopJsonRepositoryError("内容标识与资源标识不一致")
        with self._lock:
            documents = self.load_all()
            if resource_id not in documents:
                raise DesktopJsonRepositoryError("桌面资源不存在")
            current_version = self._versions.get(resource_id, 1)
            if current_version != expected_version:
                raise DesktopJsonRepositoryError("内容已更新，请重新加载")
            documents[resource_id] = document
            self._versions[resource_id] = current_version + 1
            self.replace_all(documents)
            return self._versioned(resource_id, document)

    def soft_delete(self, context: WorkspaceContext, resource_id: str) -> None:
        del context
        with self._lock:
            documents = self.load_all()
            if documents.pop(resource_id, None) is None:
                raise DesktopJsonRepositoryError("桌面资源不存在")
            self._versions.pop(resource_id, None)
            self.replace_all(documents)

    def _versioned(
        self,
        resource_id: str,
        document: DocumentT,
    ) -> VersionedDocument[DocumentT]:
        return VersionedDocument(
            document=document,
            version=self._versions.get(resource_id, 1),
            schema_version=self.SCHEMA_VERSION,
        )

    @staticmethod
    def _document_id(document: DocumentT) -> str:
        resource_id = getattr(document, "id", None)
        if not isinstance(resource_id, str) or not resource_id:
            raise DesktopJsonRepositoryError("桌面文档缺少有效标识")
        return resource_id

    def _write_json(self, payload: object) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                json.dump(payload, temporary, indent=2, ensure_ascii=False)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self.path)
        except Exception as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise DesktopJsonRepositoryError(
                f"无法保存桌面文档文件：{self.path}"
            ) from exc


class DesktopProjectRepository(DesktopJsonDocumentRepository[Script]):
    def __init__(self, path: str | os.PathLike[str] = "output/projects.json") -> None:
        super().__init__(path, Script)


class DesktopSeriesRepository(DesktopJsonDocumentRepository[Series]):
    def __init__(self, path: str | os.PathLike[str] = "output/series.json") -> None:
        super().__init__(path, Series)


class DesktopAssetLibraryRepository:
    """Desktop adapter for the legacy aggregate asset-library JSON file."""

    def __init__(self, path: str | os.PathLike[str] = "output/library_assets.json") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def set_path(self, path: str | os.PathLike[str]) -> None:
        with self._lock:
            next_path = Path(path)
            if next_path != self.path:
                self.path = next_path

    def load(self) -> GlobalAssetLibrary:
        with self._lock:
            if not self.path.exists():
                return GlobalAssetLibrary()
            try:
                return GlobalAssetLibrary.model_validate_json(
                    self.path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                raise DesktopJsonRepositoryError(
                    f"无法读取桌面资产库文件：{self.path}"
                ) from exc

    def save(self, library: GlobalAssetLibrary) -> None:
        document_repository = DesktopJsonDocumentRepository(
            self.path,
            GlobalAssetLibrary,
        )
        document_repository._write_json(library.model_dump(mode="json"))
