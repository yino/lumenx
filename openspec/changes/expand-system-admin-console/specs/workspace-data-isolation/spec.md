## ADDED Requirements

### Requirement: Explicit audited administrator inspection path
The cloud system SHALL provide a dedicated read-only inspection path for an authenticated independent administrator session that requires explicit target user/workspace scope, validates target ownership, uses transaction-local `app.current_admin_id`, and audits purpose-bound sensitive reads under the independent administrator ID. This path MUST remain separate from normal user sessions and repositories and MUST NOT grant one normal user access to another user's resources.

#### Scenario: Administrator inspects target metadata
- **WHEN** a platform administrator requests a safe resource projection for an explicitly selected user and owned workspace
- **THEN** the administrator inspection repository returns only that target scope and normal user repository behavior remains unchanged

#### Scenario: Normal user calls inspection path
- **WHEN** a client with only a valid normal-user session invokes an administrator resource-inspection endpoint
- **THEN** the server denies access before establishing administrator database context or revealing target existence

#### Scenario: Sensitive inspection lacks a purpose
- **WHEN** an administrator requests full user-authored content or private media preview without a nonempty purpose
- **THEN** the request is rejected and no sensitive content or signed URL is returned

## MODIFIED Requirements

### Requirement: Defense-in-depth database isolation
Cloud repositories MUST require an explicit user/workspace context, PostgreSQL row-level security MUST enforce the current transaction's user scope for user-owned records, and dedicated administrator repositories MUST require an authenticated `AdminContext`, transaction-local `app.current_admin_id`, and explicit target predicates. Normal user sessions MUST NOT establish administrator context. The application database role MUST remain non-superuser and without `BYPASSRLS`.

#### Scenario: Repository call omits scope
- **WHEN** cloud code attempts to call a normal resource repository without user context
- **THEN** the repository interface rejects the call before querying data

#### Scenario: Application query is accidentally under-scoped
- **WHEN** a normal user database query omits an application-level user predicate
- **THEN** row-level security still prevents rows belonging to another user from being returned or changed

#### Scenario: Administrator query omits target predicate
- **WHEN** an administrator inspection repository is called without its required target user or workspace predicate
- **THEN** the repository rejects the call rather than returning all rows permitted by administrator RLS context

#### Scenario: Pooled connection is reused
- **WHEN** a database connection serves administrator and user requests sequentially
- **THEN** transaction-local user, workspace, administrator ID, session, and token-reset context is cleared so no later request inherits prior visibility

### Requirement: Workspace asset scopes
The asset system SHALL support project, series, workspace, and system scopes. User-created workspace assets SHALL be visible only inside their owning workspace, and system assets including platform scene templates SHALL have no user/workspace owner, SHALL be mutable only by a platform administrator, and SHALL be read-only to normal users.

#### Scenario: Workspace asset is reused
- **WHEN** a project in a workspace resolves an asset stored at workspace scope
- **THEN** the asset is available according to the resolver priority without being copied from another workspace

#### Scenario: System scene is browsed
- **WHEN** an authenticated active user lists enabled system scene templates
- **THEN** the user receives safe read-only system projections without gaining mutation access or another user's assets

#### Scenario: System asset is edited by a user
- **WHEN** a non-administrator attempts to mutate a system asset
- **THEN** the server rejects the mutation

### Requirement: Private media namespace
Cloud-generated and uploaded user binaries MUST be stored in private object storage under a user/workspace namespace, platform-owned system binaries MUST be stored under a separate private system namespace, and every binary MUST be represented by authorized `media_objects` metadata whose scope and ownership satisfy database constraints.

#### Scenario: User media is uploaded
- **WHEN** a user uploads media into a project
- **THEN** the system stores it under that user's workspace prefix and records user/workspace ownership, content type, size, checksum, and provenance

#### Scenario: Administrator uploads system scene media
- **WHEN** a platform administrator uploads validated media for a system scene
- **THEN** the system stores it under the private system prefix with system scope and no user/workspace owner

#### Scenario: Arbitrary object key is submitted
- **WHEN** a browser submits an OSS key or local filesystem path instead of an authorized media ID
- **THEN** the cloud API rejects the reference

### Requirement: Authorized media delivery
The cloud system SHALL authorize each media access before issuing a short-lived signed URL or stream, MUST NOT publicly mount a shared output directory, and MUST distinguish owner access, read-only enabled system-media access, and purpose-audited administrator preview.

#### Scenario: Owner requests media
- **WHEN** an authenticated user requests a media object in an owned workspace
- **THEN** the server returns a short-lived access URL or streams the authorized object

#### Scenario: User requests enabled system scene media
- **WHEN** an authenticated user requests cover media referenced by an enabled system scene
- **THEN** the server returns short-lived read-only access without exposing the system object key

#### Scenario: Administrator previews user media
- **WHEN** a platform administrator supplies a reason and valid target user/workspace scope for private media
- **THEN** the server audits the access and returns a short-lived inline preview

#### Scenario: Another user requests media
- **WHEN** a user requests a media ID owned by another user
- **THEN** the server returns not found and does not issue an object-store signature
