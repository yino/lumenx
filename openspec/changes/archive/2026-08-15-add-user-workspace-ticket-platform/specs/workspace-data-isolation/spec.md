## ADDED Requirements

### Requirement: User-owned workspaces
The system SHALL allow an authenticated user to create and manage multiple workspaces, and every workspace SHALL belong to exactly one user.

#### Scenario: User creates another workspace
- **WHEN** an authenticated active user submits a valid workspace name
- **THEN** the system creates a workspace owned by that user without exposing it to any other user

#### Scenario: Workspace list is loaded
- **WHEN** an authenticated user lists workspaces
- **THEN** the response contains only workspaces owned by that user

### Requirement: Explicit resource scope
Every cloud project, series, script document, asset, media object, AI task, import batch, and usage event MUST be associated with the authenticated `user_id` and, where content-scoped, a workspace owned by that user.

#### Scenario: Resource is created
- **WHEN** an authenticated user creates a project in an owned workspace
- **THEN** the server derives and persists the user and workspace scope from the authenticated context rather than accepting ownership fields from the browser

### Requirement: Cross-user and cross-workspace denial
All cloud resource reads and mutations MUST query by authenticated user, workspace, and resource identifier, and MUST return a nondisclosing not-found response for resources outside that scope.

#### Scenario: Another user's project ID is guessed
- **WHEN** a user requests a project identifier owned by another user
- **THEN** the server returns not found and reveals no project metadata

#### Scenario: Project from another workspace is requested
- **WHEN** a user requests a project using a different owned workspace context
- **THEN** the server returns not found and performs no mutation

### Requirement: Defense-in-depth database isolation
Cloud repositories MUST require an explicit user/workspace context, and PostgreSQL row-level security MUST enforce the current transaction's user scope for user-owned records.

#### Scenario: Repository call omits scope
- **WHEN** cloud code attempts to call a resource repository without user context
- **THEN** the repository interface rejects the call before querying data

#### Scenario: Application query is accidentally under-scoped
- **WHEN** a database query omits an application-level user predicate
- **THEN** row-level security still prevents rows belonging to another user from being returned or changed

#### Scenario: Pooled connection is reused
- **WHEN** a database connection serves requests from two different users sequentially
- **THEN** transaction-local security context is reset so the second request cannot inherit the first user's scope

### Requirement: Workspace asset scopes
The asset system SHALL support project, series, workspace, and system scopes. User-created workspace assets SHALL be visible only inside their owning workspace, and system assets SHALL be read-only to non-administrators.

#### Scenario: Workspace asset is reused
- **WHEN** a project in a workspace resolves an asset stored at workspace scope
- **THEN** the asset is available according to the resolver priority without being copied from another workspace

#### Scenario: System asset is edited by a user
- **WHEN** a non-administrator attempts to mutate a system asset
- **THEN** the server rejects the mutation

### Requirement: Controlled asset promotion and copying
Moving an asset to a broader permitted scope MUST create an authorized record with provenance and MUST NOT transfer ownership through path manipulation.

#### Scenario: Project asset is promoted to workspace
- **WHEN** the owner promotes a project asset to the same workspace's asset library
- **THEN** the system creates a workspace-scoped asset, retains source provenance, and keeps all media references inside the same user/workspace boundary

#### Scenario: Cross-workspace promotion is attempted
- **WHEN** a request names a destination workspace different from the source workspace
- **THEN** the system rejects the operation even when both identifiers are syntactically valid

### Requirement: Private media namespace
Cloud-generated and uploaded binaries MUST be stored in private object storage under a user/workspace namespace and represented by authorized `media_objects` metadata.

#### Scenario: Media is uploaded
- **WHEN** a user uploads media into a project
- **THEN** the system stores it under that user's workspace prefix and records ownership, content type, size, checksum, and provenance

#### Scenario: Arbitrary object key is submitted
- **WHEN** the browser submits an OSS key or local filesystem path instead of an authorized media ID
- **THEN** the cloud API rejects the reference

### Requirement: Authorized media delivery
The cloud system SHALL authorize each media access before issuing a short-lived signed URL and MUST NOT publicly mount a shared output directory.

#### Scenario: Owner requests media
- **WHEN** an authenticated user requests a media object in an owned workspace
- **THEN** the server returns a short-lived access URL or streams the authorized object

#### Scenario: Another user requests media
- **WHEN** a user requests a media ID owned by another user
- **THEN** the server returns not found and does not issue an object-store signature

### Requirement: Optimistic content concurrency
Mutable project and series documents SHALL carry an optimistic version, and updates MUST fail with a conflict when the submitted base version is stale.

#### Scenario: Two browser sessions edit a project
- **WHEN** the second session submits an update based on an older project version
- **THEN** the server rejects the stale write without overwriting the first session's accepted change

### Requirement: Soft deletion and retention
Cloud users SHALL be able to soft-delete projects and workspaces, and the system SHALL retain recoverable records and media for 30 days before asynchronous physical cleanup.

#### Scenario: Project is deleted
- **WHEN** a user deletes an owned project
- **THEN** it disappears from normal listings, cannot start new AI tasks, and remains recoverable during the retention period

#### Scenario: Retention expires
- **WHEN** a soft-deleted resource has exceeded 30 days and has no legal or billing hold
- **THEN** the cleanup process removes eligible database payloads and unreferenced media with an audit record

### Requirement: Browser cache isolation
Frontend persisted state MUST be scoped by authenticated user and workspace and MUST be cleared or switched safely on logout or workspace change.

#### Scenario: A different user logs in on the same browser
- **WHEN** the previous user has logged out and another user authenticates
- **THEN** no cached project, asset, prompt default, or workspace state from the previous user is displayed

