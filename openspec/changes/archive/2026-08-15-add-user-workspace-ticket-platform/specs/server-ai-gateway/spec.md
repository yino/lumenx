## ADDED Requirements

### Requirement: Server-authoritative AI request
Every cloud AI operation MUST pass through the authenticated server AI gateway. The browser MAY submit content, owned media IDs, allowed creative parameters, and an idempotency key, but MUST NOT select provider routing, provider model IDs, prices, formulas, credentials, or arbitrary endpoints.

#### Scenario: Valid AI request
- **WHEN** an authenticated user submits a supported capability with owned resources and allowed parameters
- **THEN** the gateway selects active server configuration, quotes the task, reserves tickets, persists it, and enqueues only the task ID

#### Scenario: Client submits model override
- **WHEN** a cloud client includes a provider or model override
- **THEN** the server ignores or rejects the override according to API validation and never uses it for execution or pricing

### Requirement: Authorized AI inputs
The gateway MUST resolve all project, asset, and media inputs by server-owned identifiers within the authenticated user/workspace scope and MUST reject local paths, arbitrary object keys, and unauthorized remote references.

#### Scenario: Owned input media is submitted
- **WHEN** a request references an authorized media object in the task workspace
- **THEN** the worker receives a server-resolved private input reference suitable for the selected provider

#### Scenario: Foreign media ID is submitted
- **WHEN** a request references media owned by another user or workspace
- **THEN** the gateway returns not found before reserving tickets or calling a provider

### Requirement: Persistent task authority
PostgreSQL MUST contain the authoritative AI task record, configuration snapshot, ownership, billing correlation, state, attempts, and provider identifiers; Redis MUST be used only for dispatch and transient coordination.

#### Scenario: Redis data is lost
- **WHEN** Redis is restarted after task creation
- **THEN** persisted queued or running tasks remain discoverable and can be safely reconciled from PostgreSQL

### Requirement: Controlled task state machine
AI task transitions MUST follow the declared state machine and MUST use atomic compare-and-set semantics so duplicate workers cannot both advance or settle the same task.

#### Scenario: Duplicate delivery occurs
- **WHEN** two workers receive the same queued task ID
- **THEN** at most one worker acquires the executable transition and the other exits without provider submission or billing mutation

#### Scenario: Invalid transition is attempted
- **WHEN** code attempts to move a terminal task back to running
- **THEN** the state update is rejected and recorded for diagnostics

### Requirement: Server-side provider credentials
Cloud workers MUST obtain provider credentials from a server credential provider and MUST NOT persist plaintext secrets in model configuration, task payloads, logs, API responses, browser storage, or media metadata.

#### Scenario: Worker invokes a provider
- **WHEN** a worker executes an enabled model route
- **THEN** it resolves the configured secret reference within the server environment and sends the credential only to the intended provider

### Requirement: Provider submission recovery
The worker MUST persist provider request/task identifiers and billable acknowledgement state before polling or post-processing, and recovery MUST avoid blind provider resubmission.

#### Scenario: Worker crashes after provider acceptance
- **WHEN** a worker restarts after the provider accepted a request
- **THEN** recovery resumes polling or marks the task for review using the persisted provider identifier instead of submitting a duplicate generation

### Requirement: Server-side output handling
Workers SHALL download or receive provider results, validate them, store binaries through the authorized media store, create scoped media metadata, and expose only safe task/result projections to the client.

#### Scenario: AI generation succeeds
- **WHEN** the provider returns valid output
- **THEN** the worker stores the output under the task's user/workspace namespace, settles usage, and marks the task succeeded with media IDs

#### Scenario: Provider response contains sensitive diagnostics
- **WHEN** a provider failure contains request headers, credentials, or internal endpoint details
- **THEN** the server stores a restricted diagnostic form and returns a safe Chinese error to the user

### Requirement: Task status isolation
Users SHALL be able to list and poll only AI tasks belonging to their own workspaces.

#### Scenario: User polls a foreign task ID
- **WHEN** an authenticated user requests another user's task
- **THEN** the server returns not found without revealing status, provider, usage, or media

### Requirement: Cancellation semantics
The server SHALL accept cancellation only when the task and provider state allow it, SHALL persist the outcome, and SHALL coordinate billing release or settlement from confirmed provider billing state.

#### Scenario: Queued task is cancelled
- **WHEN** a user cancels an owned task before provider submission
- **THEN** the task becomes cancelled and the full ticket hold is released

#### Scenario: Provider cannot cancel running work
- **WHEN** a task has been submitted and the provider cannot confirm cancellation
- **THEN** the task remains billable/pending according to provider state and the UI explains the Chinese status without prematurely refunding the hold

### Requirement: Cloud AI endpoint closure
Cloud mode MUST disable or platform-admin-protect process environment mutation, local provider CLI login, raw log-tail access, debug configuration, and any endpoint that bypasses the AI gateway or billing flow.

#### Scenario: Normal user calls environment configuration endpoint
- **WHEN** a non-administrator requests or updates server provider configuration
- **THEN** the server denies the request and exposes no secret or process-path information

