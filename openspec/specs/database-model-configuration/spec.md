# database-model-configuration Specification

## Purpose
Define server-owned, database-backed, versioned AI model routing, metering, platform configuration, and administration controls.

## Requirements

### Requirement: Database-backed model routes
Cloud runtime model selection SHALL use versioned database model configuration rather than user project settings or repository YAML files. Each route SHALL declare capability, Chinese display name, provider, provider model ID, enabled state, priority, parameter schema, metering formula, and secret reference.

#### Scenario: Cloud service selects a model
- **WHEN** an AI gateway request names a supported capability
- **THEN** the server selects the enabled primary database route for that capability and records its immutable configuration snapshot on the task

#### Scenario: Catalog YAML changes without database activation
- **WHEN** a cloud deployment contains different catalog YAML but no administrator activates corresponding database configuration
- **THEN** existing cloud model routing remains unchanged

### Requirement: Complete capability routing
Every AI capability exposed to cloud users MUST have exactly one enabled primary route and MAY have ordered enabled fallback routes.

#### Scenario: Required capability has no primary route
- **WHEN** an administrator attempts to activate configuration with no primary route for an exposed capability
- **THEN** activation is rejected with a Chinese validation explanation

#### Scenario: Multiple primary routes are configured
- **WHEN** a configuration version declares more than one primary for the same capability
- **THEN** activation is rejected

### Requirement: Server-side parameter policy
Each model route MUST define server-owned defaults and an allowed parameter schema, and the gateway MUST reject or normalize client parameters before quoting and execution.

#### Scenario: User submits an allowed creative parameter
- **WHEN** a request supplies a value inside the active route's allowed range
- **THEN** the normalized value is included in the quote and task snapshot

#### Scenario: User submits an unsupported or out-of-range parameter
- **WHEN** a request supplies a parameter absent from the schema or outside its bounds
- **THEN** the gateway rejects the request before reserving tickets

### Requirement: Metering configuration validation
Every enabled route MUST include a bounded, schema-valid metering-token formula compatible with its capability and allowed parameters.

#### Scenario: Video pricing omits duration
- **WHEN** an administrator activates a video route whose formula cannot bound every allowed duration
- **THEN** activation fails and the route remains inactive

### Requirement: Secret separation
Model and platform configuration tables MUST NOT contain plaintext provider credentials. A route MAY contain only a validated secret reference resolved by server deployment infrastructure.

#### Scenario: Administrator submits plaintext key as config
- **WHEN** an administrator attempts to save a credential-shaped value in model or platform configuration
- **THEN** the server rejects the value and does not write it to the database or audit payload

### Requirement: Versioned atomic activation
Administrator configuration changes SHALL create a new immutable version, validate the complete effective configuration, append an audit event, and atomically mark the version active.

#### Scenario: Valid configuration is activated
- **WHEN** a platform administrator submits a complete valid version with a reason
- **THEN** new tasks use the new version while existing tasks retain their previous snapshots

#### Scenario: Validation fails
- **WHEN** any route, formula, fallback, exchange rate, or parameter schema in a proposed version is invalid
- **THEN** none of the proposed version becomes active

### Requirement: Fallback policy
The server SHALL use fallback routes only for configured retryable provider/model availability failures and MUST calculate each distinct provider attempt using an explicit configuration and billing policy.

#### Scenario: Primary model is temporarily unavailable
- **WHEN** the primary route returns an error classified as fallback-eligible before billable generation
- **THEN** the worker may use the next enabled fallback and records both attempts and the configuration actually used

#### Scenario: Primary produces a billable result
- **WHEN** the primary provider has already produced or charged for output
- **THEN** the worker does not automatically invoke a fallback as if the first attempt were free

### Requirement: Platform configuration
Schema-validated versioned platform configuration SHALL provide at least tokens-per-ticket, registration initial grant, authentication/session limits, and AI concurrency limits. Unset initial grant SHALL resolve to zero.

#### Scenario: Exchange rate is updated
- **WHEN** an administrator activates a positive `tokens_per_ticket` value
- **THEN** new task snapshots use it and previously created tasks remain unchanged

#### Scenario: Invalid global value is submitted
- **WHEN** an administrator submits a nonpositive exchange rate or concurrency limit
- **THEN** activation is rejected

### Requirement: No user model control
Cloud user APIs and interfaces MUST NOT allow users to select or override execution models, providers, endpoint routes, or model pricing. Existing project and series model settings SHALL be historical-only in cloud mode.

#### Scenario: Existing project contains model settings
- **WHEN** a migrated project is used for new cloud AI generation
- **THEN** the gateway ignores its historical model settings and uses active database routing

#### Scenario: User views task details
- **WHEN** a user opens an owned completed task
- **THEN** the interface may show the actual Chinese model name and technical ID as read-only audit information

### Requirement: Protected configuration administration
Only platform administrators SHALL be able to view nonpublic configuration, create versions, activate versions, disable routes, or alter pricing and exchange rules, and every mutation MUST require a reason and create an audit event.

#### Scenario: Normal user requests configuration administration
- **WHEN** a non-administrator calls an administrative model or platform config endpoint
- **THEN** the server denies access and reveals no secret references or internal provider routing

### Requirement: Initial database seeding
The cloud deployment SHALL provide an idempotent process to seed initial model routes from the existing catalog and SHALL require explicit validation before first activation.

#### Scenario: Seed process is rerun
- **WHEN** the same catalog version is seeded more than once
- **THEN** it does not create duplicate active routes or silently overwrite administrator changes
