## ADDED Requirements

### Requirement: Browser-origin deployed-stack acceptance
Cloud release verification MUST exercise the built static frontend and Nginx together with PostgreSQL, Redis, migrations, backend, and worker services, and all HTTP workflow calls MUST enter through the browser-facing origin.

#### Scenario: Core hosted workflow is tested
- **WHEN** the deployed-stack suite runs with deterministic test adapters
- **THEN** it verifies invitation registration, login cookies, CSRF mutation, workspace access, wallet access, media upload/access, AI task reservation/dispatch/settlement, and logout through `/api/v1`

#### Scenario: Unauthorized administration is tested
- **WHEN** a normal user calls a platform administration route through Nginx
- **THEN** the stack returns the expected JSON denial without exposing admin data or serving the SPA document

### Requirement: Edge contract regression gate
The release pipeline MUST fail when a supported cloud API returns HTML through the edge, bypasses the versioned prefix, exceeds an unintended upload limit, or lacks required forwarded metadata.

#### Scenario: Proxy coverage is incomplete
- **WHEN** a supported API path falls through to `index.html`
- **THEN** the edge contract test fails even if frontend and backend health probes individually return 200

#### Scenario: Nginx syntax is checked before backend startup
- **WHEN** the release job validates the built frontend image before a backend container exists
- **THEN** the syntax-only container supplies a harmless loopback resolution for the upstream name, runs the standard image entrypoint, and executes `nginx -t` without changing production DNS behavior

### Requirement: Multi-instance configuration propagation test
The release suite MUST prove activation behavior using distinct configuration readers representing separate backend or worker processes.

#### Scenario: Configuration changes between requests
- **WHEN** one instance caches version A, another instance activates version B, and the first instance accepts a new request
- **THEN** the new request observes version B while a task created under version A keeps snapshot A

### Requirement: Production policy wiring tests
Each editable database business policy MUST have at least one integration test proving that activating a changed value alters the intended later runtime behavior.

#### Scenario: Editable but unused field is introduced
- **WHEN** a platform configuration field is shown as editable but no runtime consumer test demonstrates its effect
- **THEN** the release policy test fails or the field must be reclassified as read-only deployment state

### Requirement: Deterministic test adapters
Fake provider and object-storage adapters used by deployed-stack tests MUST require an explicit test-only deployment flag and MUST be impossible to activate in normal cloud startup.

#### Scenario: Production attempts test adapter startup
- **WHEN** normal cloud mode is configured with a deterministic test adapter
- **THEN** startup fails before accepting traffic

### Requirement: Automated release gate
The cloud release job MUST require migration checks, backend tests, frontend logic and UI tests, type checking, production build, Compose validation, Nginx edge tests, deployed-stack acceptance, and wallet/task reconciliation before registration or new AI work can be enabled.

#### Scenario: Type checking fails but static export succeeds
- **WHEN** the frontend build produces files while the independent type-check stage fails
- **THEN** the release job fails and the image is not approved for rollout

#### Scenario: Reconciliation finds an invariant violation
- **WHEN** wallet, hold, task, usage, or ledger reconciliation reports a blocking issue
- **THEN** registration and new AI work remain disabled and the release is rejected

### Requirement: Discoverable repository command surface
The repository SHALL provide one root Makefile that documents and delegates local setup, development startup, production build, tests, static checks, database migration, Docker Compose operations, and platform desktop builds to the project's existing commands.

#### Scenario: Developer requests command help
- **WHEN** a developer runs `make` or `make help`
- **THEN** the repository prints Chinese descriptions for startup, build, test, migration, Docker, and desktop packaging commands without starting a service or changing deployment state

#### Scenario: Operator runs the local release check
- **WHEN** an operator runs `make release-check`
- **THEN** it performs only non-paid tests, static checks, production build, Compose validation, and strict OpenSpec validation without executing a real staging canary, enabling a deployment gate, or retiring compatibility routing

### Requirement: Host-path-independent local Compose
Local Docker startup SHALL inject local credentials from a Git-ignored environment file, preserve separate PostgreSQL administrator and RLS application roles, publish PostgreSQL only on loopback host port `15433`, and use Docker-managed volumes instead of bind-mounting repository secrets, outputs, or imports. Repository Docker build definitions SHALL live under `docker/` while retaining repository-root build contexts. Production Compose selection MUST continue to use deployment-managed file secrets and MUST NOT publish PostgreSQL.

#### Scenario: Repository is outside Docker Desktop shared paths
- **WHEN** a developer prepares the local environment and runs the default `docker compose up` from a repository path unavailable to the Docker VM
- **THEN** the complete local web stack starts without requesting a bind mount for any repository secret, output, or import path

#### Scenario: Production configuration is validated
- **WHEN** the release gate or an operator selects only `docker-compose.yml`
- **THEN** the local override is excluded and file-backed production secrets remain required

#### Scenario: Developer connects to local PostgreSQL
- **WHEN** a developer uses a host database client against `127.0.0.1:15433`
- **THEN** the connection reaches the local Compose PostgreSQL service while non-loopback clients cannot use that publication

#### Scenario: Docker images are built from centralized definitions
- **WHEN** Compose builds backend, frontend, migration, worker, or PostgreSQL utility images
- **THEN** it resolves Dockerfiles below `docker/` with the repository root as build context

### Requirement: Release evidence report
Each release candidate SHALL produce a Chinese evidence report containing artifact revision, migration head, test results, edge-route checks, configuration version, reconciliation results, and any real OSS/provider canary identifiers without secrets or raw user content.

#### Scenario: Release candidate passes
- **WHEN** all automated gates and staging canaries succeed
- **THEN** the report records the evidence needed for an explicit go/no-go decision rather than declaring readiness from task checkboxes alone

### Requirement: Guarded real staging canary
The real OSS/provider canary MUST separate non-billable preflight, one explicitly confirmed minimum-cost execution, and finalization. A recent sanitized manifest MUST prove PostgreSQL, Redis, OSS Bucket, and provider-account resource fingerprints differ from production, and its staging fingerprints MUST match an admin-only live deployment projection before each phase proceeds. The canary MUST NOT pass until registration, new AI database policy, and both deployment emergency gates are restored closed and reconciliation is consistent.

#### Scenario: Operator attempts a paid canary without explicit confirmation
- **WHEN** the execute phase lacks the exact isolated staging identity, reviewed model/quote boundary, or one-paid-call confirmation
- **THEN** it fails before uploading media or submitting a provider task

#### Scenario: Staging reuses a production resource
- **WHEN** any staging resource fingerprint matches the corresponding production database, Redis, OSS Bucket, or provider-account fingerprint
- **THEN** preflight and execute fail before any media or provider operation

#### Scenario: Isolation manifest targets a different staging deployment
- **WHEN** any manifest staging fingerprint is missing from or differs from the live deployment-state projection
- **THEN** preflight, execute, and finalize fail without exposing raw resource identities or credentials

#### Scenario: Canary command runs beside a different database
- **WHEN** the command's local resource fingerprints differ from the target edge deployment before direct reconciliation
- **THEN** every canary phase fails before media, provider, or local database evidence is accepted

#### Scenario: Provider canary completes but deployment remains open
- **WHEN** the provider task and settlement succeed but an AI or registration deployment emergency gate is not restored closed
- **THEN** execute evidence remains pending and finalize cannot issue a GO decision
