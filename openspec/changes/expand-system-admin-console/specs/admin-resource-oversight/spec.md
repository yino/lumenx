## ADDED Requirements

### Requirement: Explicit administrator target scope
Every cross-user administrator inspection request MUST identify a target user and, for workspace-scoped resources, an optional or required target workspace that the server verifies belongs to that user. The server SHALL derive no target scope from browser-cached user state.

#### Scenario: Valid user and workspace are selected
- **WHEN** the administrator requests resources for a workspace owned by the target user
- **THEN** the inspection service establishes that explicit target scope for one transaction and returns only matching rows

#### Scenario: User and workspace do not match
- **WHEN** the request combines identifiers from different owners
- **THEN** the server rejects the request and returns no counts, names, content, media, or task information

### Requirement: Read-only resource inspection
Administrator inspection APIs SHALL provide read-only projections for user workspaces, projects, series, script documents, characters, scenes, props, other assets, media, AI tasks, task attempts, usage, wallet/ledger, orders, and related audit events. They MUST NOT expose generic mutation, hard-delete, or ownership-transfer operations.

#### Scenario: Administrator lists a user's assets
- **WHEN** the administrator opens the user's asset tab
- **THEN** the API returns paginated metadata grouped by workspace, scope, and asset type without changing any resource

#### Scenario: Mutation is attempted through inspection route
- **WHEN** a client sends an unsupported update or delete method to a resource-inspection endpoint
- **THEN** the server rejects it and performs no domain mutation

### Requirement: Safe resource summaries
Default inspection projections SHALL mask phone numbers and omit full script bodies, hidden prompts, raw object keys, provider credentials, internal diagnostics, and unrestricted result payloads while returning identifiers, ownership, names, statuses, counts, content types, sizes, safe errors, provenance summaries, and timestamps needed for support.

#### Scenario: User overview is loaded
- **WHEN** the administrator opens user detail without requesting sensitive content
- **THEN** the response contains operational summaries and masked identity without full user-authored bodies or private storage keys

### Requirement: Purpose-bound sensitive content access
Loading full user-authored script/prompt text, restricted task diagnostics, or a private media preview MUST require an explicit resource ID and nonempty Chinese purpose, MUST reauthorize the independent administrator session and target scope, and MUST atomically append a sensitive-access audit event with the independent administrator ID.

#### Scenario: Administrator previews a script
- **WHEN** the administrator supplies a reason for viewing an owned script in the selected user/workspace scope
- **THEN** the server returns a bounded script projection and records administrator ID, target, purpose, correlation ID, and time

#### Scenario: Audit persistence fails
- **WHEN** the sensitive content could be read but its access event cannot be committed
- **THEN** the server returns no sensitive body or signed preview URL

### Requirement: Authorized administrator media preview
Administrator media preview SHALL issue a short-lived inline-only access URL or authorized stream after explicit target validation and purpose auditing. Raw object keys and reusable storage credentials MUST NOT be returned.

#### Scenario: Administrator previews user media
- **WHEN** the media belongs to the selected user/workspace and the administrator provides a reason
- **THEN** the server issues a short-lived inline preview and audits the access

#### Scenario: Preview scope is stale
- **WHEN** the media no longer belongs to the submitted target or has been deleted/quarantined
- **THEN** the server refuses to sign or stream it and reveals no storage location

### Requirement: Administrator AI task detail
The console SHALL provide task detail containing target ownership, project context, state transitions, attempts, safe provider/model identifiers, configuration and metering snapshots, holds, usage/settlement correlations, result media IDs, safe errors, and support-review state. Secrets and unrestricted provider diagnostics MUST remain hidden.

#### Scenario: Support-review task is opened
- **WHEN** the administrator selects a task requiring support review
- **THEN** the console displays the persisted provider-billing acknowledgement, settlement state, safe failure context, and correlated wallet records needed for a decision

### Requirement: Operational exception queues
The system SHALL provide filterable queues for failed/stalled AI tasks, support-review tasks, stale holds, order/ledger reconciliation failures, missing media, and other declared integrity exceptions, with links to the affected user and resource.

#### Scenario: Reconciliation reports an order mismatch
- **WHEN** a manual recharge reconciliation run reports a high-severity mismatch
- **THEN** the dashboard exception queue includes one deduplicated item linked to the order, user, wallet, and report

### Requirement: Resource query boundaries
Every administrator resource list SHALL use deterministic pagination, bounded date/search filters, explicit user/workspace predicates, indexed ordering, and response field allowlists.

#### Scenario: Administrator requests an unbounded asset scan
- **WHEN** a request omits required pagination or exceeds configured limits
- **THEN** the server applies safe limits or rejects the request instead of loading every platform asset

### Requirement: No administrator impersonation
The console and APIs MUST NOT issue a user session from an administrator session, rewrite an `AdminPrincipal` as a target `UserPrincipal`, equate an administrator ID with a user ID, or call normal user routes with forged ownership context to inspect resources.

#### Scenario: Client requests login-as behavior
- **WHEN** an administrator client attempts to obtain a session for a target user
- **THEN** the server rejects the operation because impersonation is unavailable in the first release

### Requirement: Inspection audit search
The administrator SHALL be able to search privileged inspection events by independent administrator ID, target user, workspace, resource type, resource ID, action, purpose, correlation ID, and bounded date range.

#### Scenario: Sensitive access is reviewed
- **WHEN** the administrator filters audit history for media previews of one user
- **THEN** every matching preview event includes the administrator ID, purpose, exact target identifiers, and timestamp without exposing the media itself
