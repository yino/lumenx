from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from .ai_dispatch import CeleryTaskDispatcher
from .asset_repositories import PostgresWorkspaceAssetRepository
from .cloud_playground import CloudPlaygroundService
from .configuration_service import ConfigurationService
from .content_repositories import PostgresProjectRepository, PostgresSeriesRepository
from .credentials import EnvironmentCredentialProvider
from .desktop_adapters import (
    DesktopCatalogModelConfigurationProvider,
    DesktopCredentialProvider,
    DesktopMediaStorage,
    InProcessTaskDispatcher,
    LocalIdentityContextProvider,
    NoOpDesktopBillingService,
)
from .desktop_repositories import (
    DesktopAssetLibraryRepository,
    DesktopProjectRepository,
    DesktopSeriesRepository,
)
from .media_storage import CloudMediaStorage, OSSPrivateObjectStore
from .model_routing import DatabaseModelConfigurationProvider
from .settings import DeploymentMode, DeploymentSettings
from .ticket_reservation import TicketReservationService
from .ticket_retry import RetryAccountingService
from .ticket_settlement import TicketSettlementService


Factory = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class RepositoryAdapterSet:
    projects: Factory
    series: Factory
    asset_library: Factory
    playground: Factory


@dataclass(frozen=True, slots=True)
class BillingAdapterSet:
    mode: Literal["disabled", "required"]
    service: Factory
    reservation: Factory | None = None
    settlement: Factory | None = None
    retry: Factory | None = None


@dataclass(frozen=True, slots=True)
class DeploymentAdapterSet:
    mode: DeploymentMode
    identity: Factory | None
    repositories: RepositoryAdapterSet
    media: Factory
    credentials: Factory
    model_configuration: Factory
    tasks: Factory
    billing: BillingAdapterSet
    legacy_pipeline_enabled: bool
    local_static_files_enabled: bool
    cloud_authentication_required: bool

    @property
    def is_desktop(self) -> bool:
        return self.mode is DeploymentMode.DESKTOP


def compose_deployment_adapters(settings: DeploymentSettings) -> DeploymentAdapterSet:
    if settings.deployment_mode is DeploymentMode.DESKTOP:
        return DeploymentAdapterSet(
            mode=DeploymentMode.DESKTOP,
            identity=LocalIdentityContextProvider,
            repositories=RepositoryAdapterSet(
                projects=DesktopProjectRepository,
                series=DesktopSeriesRepository,
                asset_library=DesktopAssetLibraryRepository,
                playground=_desktop_playground_repository,
            ),
            media=DesktopMediaStorage,
            credentials=DesktopCredentialProvider,
            model_configuration=DesktopCatalogModelConfigurationProvider,
            tasks=InProcessTaskDispatcher,
            billing=BillingAdapterSet(
                mode="disabled",
                service=NoOpDesktopBillingService,
            ),
            legacy_pipeline_enabled=True,
            local_static_files_enabled=True,
            cloud_authentication_required=False,
        )

    if settings.deployment_mode is DeploymentMode.TEST:
        from .test_adapters import DeterministicPrivateObjectStore, require_test_adapters

        require_test_adapters(settings)
        media_factory = lambda database: CloudMediaStorage(
            database,
            DeterministicPrivateObjectStore(settings),
            namespace_prefix="release-test",
        )
    else:
        media_factory = lambda database: CloudMediaStorage(
            database,
            OSSPrivateObjectStore(settings),
        )

    return DeploymentAdapterSet(
        mode=settings.deployment_mode,
        identity=None,
        repositories=RepositoryAdapterSet(
            projects=PostgresProjectRepository,
            series=PostgresSeriesRepository,
            asset_library=PostgresWorkspaceAssetRepository,
            playground=CloudPlaygroundService,
        ),
        media=media_factory,
        credentials=lambda: EnvironmentCredentialProvider(
            settings.provider_secret_ref_names
        ),
        model_configuration=lambda database, identity: DatabaseModelConfigurationProvider(
            ConfigurationService(database),
            identity,
        ),
        tasks=CeleryTaskDispatcher,
        billing=BillingAdapterSet(
            mode="required",
            service=TicketReservationService,
            reservation=TicketReservationService,
            settlement=TicketSettlementService,
            retry=RetryAccountingService,
        ),
        legacy_pipeline_enabled=False,
        local_static_files_enabled=False,
        cloud_authentication_required=True,
    )


def _desktop_playground_repository():
    from src.apps.playground.storage import DesktopPlaygroundRepository

    return DesktopPlaygroundRepository()
