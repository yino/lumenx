from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, Mapping, Protocol, Sequence, TypeVar, runtime_checkable

from pydantic import SecretStr


DocumentT = TypeVar("DocumentT")
GenerationT = TypeVar("GenerationT")
TemplateT = TypeVar("TemplateT")


@dataclass(frozen=True, slots=True)
class UserContext:
    user_id: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class AdminContext:
    admin_id: str
    session_id: str | None = None
    username: str | None = None


@dataclass(frozen=True, slots=True)
class SystemContext:
    service_name: str


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    identity: UserContext
    workspace_id: str


@dataclass(frozen=True, slots=True)
class VersionedDocument(Generic[DocumentT]):
    document: DocumentT
    version: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class MediaWrite:
    content: bytes
    content_type: str
    filename: str
    project_id: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredMedia:
    media_id: str
    object_key: str
    content_type: str
    size_bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class ModelRouteSnapshot:
    config_version_id: str
    route_id: str
    capability: str
    provider: str
    provider_model_id: str
    display_name: str
    parameters: Mapping[str, Any]
    metering_formula: Mapping[str, Any]
    fallback_policy: Mapping[str, Any]
    secret_ref: str


@dataclass(frozen=True, slots=True)
class TicketReservation:
    hold_id: str
    quoted_microtickets: int
    tokens_per_ticket: int


@dataclass(frozen=True, slots=True)
class UsageSettlement:
    task_id: str
    metering_tokens: int
    raw_usage: Mapping[str, Any]
    billable: bool = True


@runtime_checkable
class IdentityContextProvider(Protocol):
    def current_user(self) -> UserContext: ...


@runtime_checkable
class ScopedRepository(Protocol, Generic[DocumentT]):
    def list(self, context: WorkspaceContext) -> Sequence[VersionedDocument[DocumentT]]: ...

    def get(
        self,
        context: WorkspaceContext,
        resource_id: str,
    ) -> VersionedDocument[DocumentT] | None: ...

    def add(
        self,
        context: WorkspaceContext,
        document: DocumentT,
    ) -> VersionedDocument[DocumentT]: ...

    def update(
        self,
        context: WorkspaceContext,
        resource_id: str,
        document: DocumentT,
        expected_version: int,
    ) -> VersionedDocument[DocumentT]: ...

    def soft_delete(self, context: WorkspaceContext, resource_id: str) -> None: ...


@runtime_checkable
class PlaygroundRepository(Protocol, Generic[GenerationT, TemplateT]):
    def add_generation(self, generation: GenerationT) -> None: ...

    def get_generation(self, generation_id: str) -> GenerationT | None: ...

    def list_history(self, limit: int = 50, offset: int = 0) -> Sequence[GenerationT]: ...

    def update_generation(self, generation: GenerationT) -> None: ...

    def delete_generation(self, generation_id: str) -> bool: ...

    def add_template(self, template: TemplateT) -> None: ...

    def get_template(self, template_id: str) -> TemplateT | None: ...

    def list_templates(self) -> Sequence[TemplateT]: ...

    def update_template(self, template: TemplateT) -> None: ...

    def delete_template(self, template_id: str) -> bool: ...


@runtime_checkable
class AggregateRepository(Protocol, Generic[DocumentT]):
    def load(self) -> DocumentT: ...

    def save(self, document: DocumentT) -> None: ...


@runtime_checkable
class MediaStorage(Protocol):
    def store(self, context: WorkspaceContext, media: MediaWrite) -> StoredMedia: ...

    def authorized_url(
        self,
        context: WorkspaceContext,
        media_id: str,
        expires_at: datetime,
    ) -> str: ...

    def delete(self, context: WorkspaceContext, media_id: str) -> None: ...


@runtime_checkable
class CredentialProvider(Protocol):
    def resolve(self, secret_ref: str) -> SecretStr: ...


@runtime_checkable
class ModelConfigurationProvider(Protocol):
    def select_route(
        self,
        capability: str,
        requested_parameters: Mapping[str, Any],
    ) -> ModelRouteSnapshot: ...


@runtime_checkable
class TaskDispatcher(Protocol):
    def dispatch(self, task_id: str) -> None: ...


@runtime_checkable
class BillingService(Protocol):
    def reserve(
        self,
        context: WorkspaceContext,
        task_id: str,
        maximum_metering_tokens: int,
        tokens_per_ticket: int,
    ) -> TicketReservation: ...

    def settle(self, context: WorkspaceContext, settlement: UsageSettlement) -> None: ...

    def release(self, context: WorkspaceContext, task_id: str, reason: str) -> None: ...
