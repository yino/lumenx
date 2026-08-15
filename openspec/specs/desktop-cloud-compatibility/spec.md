# desktop-cloud-compatibility Specification

## Purpose
Define explicit desktop and cloud deployment composition while preserving shared domain behavior and safe local-data migration.

## Requirements

### Requirement: Explicit deployment composition
The application SHALL compose identity, repository, media storage, model configuration, credential, task runner, and billing adapters from an explicit `desktop` or `cloud` deployment mode at startup.

#### Scenario: Desktop mode starts
- **WHEN** the packaged desktop application starts in desktop mode
- **THEN** it uses local adapters and does not require PostgreSQL, Redis, OSS, or cloud authentication

#### Scenario: Cloud mode starts
- **WHEN** the service starts in cloud mode
- **THEN** it refuses readiness until required database, queue, object storage, model configuration, and credential dependencies are valid

### Requirement: Domain services remain deployment-neutral
Shared generation domain services MUST depend on adapter interfaces and MUST NOT branch on deployment mode inside individual business operations.

#### Scenario: Project is created in either mode
- **WHEN** desktop and cloud adapters execute the shared create-project service contract
- **THEN** domain validation and generated project structure remain behaviorally compatible while persistence and ownership enforcement come from the selected adapters

### Requirement: Desktop local ownership behavior
Desktop mode SHALL use a local principal, existing local persistence/media behavior through adapters, and local user-supplied provider credentials. It SHALL NOT require ticket charging because it does not use platform credentials.

#### Scenario: Desktop user generates media
- **WHEN** a local user has configured a local provider credential
- **THEN** generation runs through the local adapter without a cloud wallet or platform secret

### Requirement: Cloud mandatory controls
Cloud mode MUST require authenticated user/workspace scope, database model routing, private media storage, persistent tasks, platform credentials, and ticket reservation for every billable AI operation.

#### Scenario: Cloud billing adapter is unavailable
- **WHEN** cloud mode cannot create a required ticket hold
- **THEN** it rejects the AI operation and does not fall back to unmetered local execution

### Requirement: Platform secret exclusion from desktop
Desktop packages MUST NOT contain cloud platform provider credentials, secret-manager access, or a fallback path that calls cloud-paid providers without authenticated ticket accounting.

#### Scenario: Desktop artifact is inspected
- **WHEN** a packaged desktop build is examined
- **THEN** no cloud provider secret or unrestricted platform AI credential is present

### Requirement: Local import targeting
The system SHALL provide an administrator-controlled importer that requires an explicit target user and owned workspace for existing local projects, series, asset libraries, Playground history selected for migration, and media.

#### Scenario: Import target is valid
- **WHEN** an administrator selects an existing user and that user's workspace
- **THEN** imported records and media are assigned only to that user/workspace and tagged with one import batch

#### Scenario: Workspace belongs to another user
- **WHEN** the supplied target user and workspace ownership do not match
- **THEN** the importer rejects the batch before writing data

### Requirement: Import dry run and validation
The importer MUST support a non-mutating dry run that validates source JSON schemas, references, filenames, checksums, storage capacity, and identifier conflicts and reports planned changes in Chinese.

#### Scenario: Source contains broken media reference
- **WHEN** dry run finds a referenced local file that is missing
- **THEN** the report identifies the affected record and the real import does not start until the configured resolution policy is accepted

### Requirement: Idempotent import
Import execution MUST be resumable and idempotent by import batch and source fingerprint so retrying an interrupted import does not duplicate projects, assets, ledger entries, or media objects.

#### Scenario: Import is retried after interruption
- **WHEN** the administrator retries the same source fingerprint and target after a partial failure
- **THEN** completed items are verified/reused and only incomplete items continue

### Requirement: Import integrity
The importer SHALL preserve project/series relationships, asset provenance, content schema versions, and media checksums and SHALL produce a final reconciliation report.

#### Scenario: Import succeeds
- **WHEN** every source item is migrated and verified
- **THEN** the batch report accounts for all projects, series, assets, tasks selected for history, and media with no unscoped record

### Requirement: Import rollback
An administrator SHALL be able to revert an import batch without deleting pre-existing or subsequently referenced user data.

#### Scenario: Batch is reverted
- **WHEN** an administrator confirms rollback with a reason
- **THEN** records created exclusively by that batch are soft-deleted, unreferenced imported media is scheduled for cleanup, and the rollback is audited

### Requirement: Adapter contract verification
Automated contract tests SHALL exercise desktop and cloud implementations of shared repository, storage, task runner, credential, model-config, and billing interfaces.

#### Scenario: Adapter behavior diverges
- **WHEN** an adapter violates a shared contract such as resource creation, media resolution, or terminal task persistence
- **THEN** the contract test suite fails before release
