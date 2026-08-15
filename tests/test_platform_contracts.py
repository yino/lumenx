from src.platform.contracts import (
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
    WorkspaceContext,
)


def test_workspace_context_keeps_identity_and_scope_together() -> None:
    identity = UserContext(user_id="user-1", session_id="session-1")
    context = WorkspaceContext(identity=identity, workspace_id="workspace-1")

    assert context.identity.user_id == "user-1"
    assert context.workspace_id == "workspace-1"


def test_platform_boundaries_are_runtime_checkable_protocols() -> None:
    protocols = (
        IdentityContextProvider,
        ScopedRepository,
        AggregateRepository,
        PlaygroundRepository,
        MediaStorage,
        CredentialProvider,
        ModelConfigurationProvider,
        TaskDispatcher,
        BillingService,
    )

    assert all(getattr(protocol, "_is_runtime_protocol", False) for protocol in protocols)
