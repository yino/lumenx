## ADDED Requirements

### Requirement: Platform-owned scene templates
The system SHALL represent every platform scene template as an asset with `scope=system` and `asset_type=scene`, no user/workspace owner, an auto-increment integer ID, validated scene payload, optimistic version, lifecycle state, provenance, and timestamps.

#### Scenario: Administrator creates a scene template
- **WHEN** the administrator submits valid Chinese display content, generation fields, category, tags, visibility, sort order, optional system cover media, and reason
- **THEN** the system creates one system scene and an audit event linked to the independent administrator ID without assigning it to a user workspace

#### Scenario: Normal user creates a system scene
- **WHEN** a non-administrator submits a system-scoped asset mutation
- **THEN** the server denies the request and creates no asset or media record

### Requirement: Validated scene schema
System scene payloads MUST validate required Chinese name and description, bounded category/tags, prompt and negative-prompt fields, supported aspect/style metadata, visibility state, sort order, schema version, and media compatibility before persistence.

#### Scenario: Scene payload is invalid
- **WHEN** a scene contains unsupported fields, excessive text, an invalid sort order, or incompatible cover media
- **THEN** the server returns field-level Chinese validation errors and writes nothing

### Requirement: Versioned scene editing
The independently authenticated administrator SHALL be able to update a system scene only from its current optimistic version, and each accepted edit SHALL increment the version and append an audit event with the independent administrator ID and safe before/after metadata.

#### Scenario: Current scene is edited
- **WHEN** the administrator submits changes against the current version with a reason
- **THEN** the scene is updated, its version increments once, and prior user copies remain unchanged

#### Scenario: Stale scene edit is submitted
- **WHEN** another edit has already advanced the scene version
- **THEN** the server rejects the stale update without overwriting the newer scene

### Requirement: Scene visibility and archival
The administrator SHALL be able to enable, disable, reorder, and soft-archive system scenes. Disabled or archived scenes MUST NOT appear for new user selection, while existing copied user assets and historical provenance remain valid.

#### Scenario: Scene is disabled
- **WHEN** the administrator disables an enabled template with a reason
- **THEN** it disappears from new user catalog queries but remains visible in administrator history and existing project provenance

#### Scenario: Scene is archived
- **WHEN** the administrator confirms archival with a reason
- **THEN** the system sets archival metadata and does not physically delete the scene or referenced media

### Requirement: System scene media
System scene cover binaries MUST be stored as private system-scoped media under a platform namespace, with checksum, MIME type, size, lifecycle, and provenance metadata. Raw object keys MUST NOT be exposed.

#### Scenario: Administrator uploads a scene cover
- **WHEN** a supported image passes size, type, and checksum validation
- **THEN** it is stored under the private system-media namespace and linked to the scene through authorized metadata

#### Scenario: Unsupported cover is uploaded
- **WHEN** a file fails type, size, or content validation
- **THEN** the upload is rejected or quarantined and cannot be attached to an enabled scene

### Requirement: Read-only user catalog
Authenticated active users SHALL be able to list and view only enabled, nonarchived system scenes in deterministic category/sort order, and SHALL NOT be able to mutate the system record or system media.

#### Scenario: User browses system scenes
- **WHEN** an authenticated user requests the scene catalog
- **THEN** the response contains enabled safe scene projections and short-lived authorized cover access without administrator metadata

### Requirement: Snapshot copy into user scope
Using a system scene in a project or workspace SHALL create a user-owned asset copy containing the source system scene ID, source version, immutable source snapshot, and media provenance. Later system-scene changes MUST NOT silently alter that copy.

#### Scenario: User applies a scene template
- **WHEN** a user selects an enabled scene for an owned project
- **THEN** the system creates an owned project/workspace scene asset with source provenance inside the same user/workspace boundary

#### Scenario: Source scene changes later
- **WHEN** an administrator edits or disables the source after a user copied it
- **THEN** the user's existing copy retains its accepted snapshot and remains usable according to normal asset rules

### Requirement: System scene administrator directory
The console SHALL provide paginated search and filters for scene ID, name, category, tag, visibility, lifecycle, schema version, and modification period, with deterministic ordering and cover preview.

#### Scenario: Administrator filters disabled scenes
- **WHEN** the administrator selects the disabled filter
- **THEN** only disabled nonarchived scenes are returned with version, usage count, independent administrator update actor, and update time

### Requirement: Scene usage protection
The system MUST report usage counts and active references before disabling or archiving a system scene and MUST require explicit confirmation when references exist.

#### Scenario: Referenced scene is archived
- **WHEN** the administrator confirms archival after reviewing reference counts
- **THEN** no existing user asset is deleted or rewritten and the action records the reported counts in its audit metadata
