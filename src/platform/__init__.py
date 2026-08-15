"""Deployment composition primitives for desktop and cloud runtimes."""

from .contracts import (
    AggregateRepository,
    BillingService,
    CredentialProvider,
    IdentityContextProvider,
    MediaStorage,
    ModelConfigurationProvider,
    PlaygroundRepository,
    ScopedRepository,
    TaskDispatcher,
    UserContext,
    VersionedDocument,
    WorkspaceContext,
)
from .credentials import CredentialResolutionError, EnvironmentCredentialProvider
from .desktop_adapters import (
    DesktopCredentialProvider,
    DesktopMediaStorage,
    InProcessTaskDispatcher,
    LocalIdentityContextProvider,
    NoOpDesktopBillingService,
)
from .settings import DeploymentMode, DeploymentSettings, get_deployment_settings

__all__ = [
    "AggregateRepository",
    "BillingService",
    "CredentialProvider",
    "CredentialResolutionError",
    "DesktopCredentialProvider",
    "DesktopMediaStorage",
    "DeploymentMode",
    "DeploymentSettings",
    "IdentityContextProvider",
    "InProcessTaskDispatcher",
    "LocalIdentityContextProvider",
    "EnvironmentCredentialProvider",
    "MediaStorage",
    "ModelConfigurationProvider",
    "NoOpDesktopBillingService",
    "PlaygroundRepository",
    "ScopedRepository",
    "TaskDispatcher",
    "UserContext",
    "VersionedDocument",
    "WorkspaceContext",
    "get_deployment_settings",
]
