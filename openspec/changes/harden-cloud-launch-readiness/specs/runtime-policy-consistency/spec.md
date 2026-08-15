## ADDED Requirements

### Requirement: Explicit configuration authority
Every exposed setting MUST declare whether PostgreSQL active configuration or deployment configuration is authoritative, and the administration UI MUST NOT present deployment-managed infrastructure capacity as an immediately mutable business setting.

#### Scenario: Administrator views worker concurrency
- **WHEN** the administration UI displays global Celery worker concurrency
- **THEN** it identifies the value as deployment-managed and read-only, including any detected mismatch, rather than saving it in an activatable business policy

#### Scenario: Secret or endpoint is configured
- **WHEN** an operator changes a database, Redis, OSS, provider credential, or trusted-proxy setting
- **THEN** the value is supplied through deployment configuration and never persisted in platform configuration rows

### Requirement: Version-aware active configuration
New runtime operations MUST observe the active PostgreSQL configuration version after its activation transaction commits, across separate backend instances and worker processes, without requiring process restart.

#### Scenario: Model version is activated
- **WHEN** an administrator activates a new model route and a different backend instance accepts a later AI request
- **THEN** the later request snapshots the newly active route and does not reuse an indefinitely cached prior version

#### Scenario: Exchange rate changes
- **WHEN** an administrator activates a new `tokens_per_ticket` value
- **THEN** new tasks use the new rate while tasks created before activation settle with their original rate snapshot

### Requirement: Database-backed registration policy
Registration grant, registration mode, session idle lifetime, session absolute lifetime, and maximum sessions per user MUST be resolved from the active database configuration for each new registration or login.

#### Scenario: Initial grant changes
- **WHEN** an administrator activates a different registration grant and a user registers afterward
- **THEN** the new wallet and immutable grant ledger entry use the activated amount and record its configuration version

#### Scenario: Session policy changes
- **WHEN** a new session is issued after session lifetimes are changed
- **THEN** its stored idle and absolute expiry use the new policy while existing sessions keep their previously stored expiries

#### Scenario: Session count reaches limit
- **WHEN** successful authentication would exceed the active maximum session count
- **THEN** the system enforces the limit transactionally and records which oldest eligible sessions were revoked

### Requirement: Database-backed operational policy
Media URL lifetime, soft-delete retention, and stale-hold thresholds MUST be resolved from the active database configuration by the operation that applies them, and the applied version MUST be available for audit or reconciliation.

#### Scenario: Media URL policy changes
- **WHEN** a user requests media access after a shorter signed-URL lifetime is activated
- **THEN** the server-issued expiry does not exceed the active lifetime even if the client requests a longer value

#### Scenario: Maintenance job starts
- **WHEN** retention or stale-hold maintenance begins
- **THEN** the job resolves the currently active threshold once, uses it for that run, and records the configuration version in its summary

### Requirement: Immutable operation snapshots
Configuration changes MUST NOT retroactively alter existing AI task pricing/routing snapshots, session expiries, invitation decisions, or immutable ledger entries.

#### Scenario: Activation occurs during AI execution
- **WHEN** a configuration version changes after a provider has accepted a task
- **THEN** execution, recovery, and settlement continue with the task's original snapshot

### Requirement: Fail-closed configuration availability
Business operations that require active policy MUST fail with a stable Chinese service error when no complete active configuration is available and MUST NOT fall back to environment defaults that change billing or authorization behavior.

#### Scenario: Active version is missing
- **WHEN** a user attempts registration or AI task creation without a complete active policy
- **THEN** the server rejects the operation before creating user, wallet, hold, task, or provider records
