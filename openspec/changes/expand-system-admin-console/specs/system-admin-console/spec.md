## ADDED Requirements

### Requirement: Administrator-only console boundary
The system SHALL expose the system administration console and every `/api/v1/admin/*` API only to an authenticated active `admin_users` account with a valid independent administrator session. Administrator authorization MUST use an `AdminPrincipal`, `AdminContext`, and transaction-local `app.current_admin_id`; it MUST NOT derive authority from a `users` row, `users.is_platform_admin`, normal-user principal, normal-user session, or workspace membership.

#### Scenario: Normal user opens an administrator route
- **WHEN** an authenticated normal user navigates directly to an administrator URL
- **THEN** the application denies access without rendering administrator navigation or requesting platform data

#### Scenario: Normal user calls an administrator API
- **WHEN** a client sends a valid normal-user session and CSRF token directly to an `/api/v1/admin/*` endpoint
- **THEN** the server rejects it and reveals no platform totals, target-user existence, internal configuration, or user-owned content

#### Scenario: Administrator session calls a normal-user API
- **WHEN** a client sends only an administrator session to an endpoint that requires a normal user and workspace
- **THEN** the server rejects it without treating the administrator ID as a user ID or creating user-domain state

#### Scenario: Administrator service is called without administrator context
- **WHEN** application code invokes an administrator service without an `AdminContext`
- **THEN** the service rejects the operation before executing a database query

### Requirement: Independent administrator identity and session lifecycle
The system SHALL store administrator identities in `admin_users` and administrator sessions in `admin_sessions`, both with auto-increment integer primary keys and no foreign-key constraints. Administrator login, logout, session lookup, CSRF validation, expiry, revocation, password change, and rate limiting SHALL be independent from normal-user authentication and SHALL use the dedicated `lumenx_admin_session` and `lumenx_admin_csrf` cookies.

#### Scenario: Administrator signs in
- **WHEN** an active administrator submits a valid administrator username and password to `/api/v1/admin/auth/login`
- **THEN** the system creates only an `admin_sessions` record and issues the dedicated administrator cookies without creating or modifying a normal-user session

#### Scenario: Normal-user credentials are submitted to administrator login
- **WHEN** credentials are valid for a `users` account but do not match an active `admin_users` account
- **THEN** administrator login fails with a nondisclosing error and no administrator session is created

#### Scenario: Administrator session is revoked
- **WHEN** the administrator logs out, changes the administrator password, expires, or is explicitly revoked
- **THEN** the administrator cookie becomes unusable while unrelated normal-user sessions remain unchanged

### Requirement: Idempotent sole-administrator bootstrap
After database migrations, the deployment SHALL idempotently create the sole administrator in `admin_users` using configured bootstrap credentials. Local Docker Compose SHALL default to username `admin` and initial password `sk532359025`; non-local deployment SHALL require an explicit nondefault password. Bootstrap MUST NOT create a `users` row, wallet, ledger grant, workspace, project, or user-owned asset, and rerunning it MUST NOT silently overwrite an existing administrator password.

#### Scenario: Fresh local Compose stack starts
- **WHEN** migrations have completed and no administrator username `admin` exists
- **THEN** bootstrap creates exactly one active `admin_users` row whose credentials are `admin` and `sk532359025` and creates no user-domain records for it

#### Scenario: Bootstrap is rerun
- **WHEN** the configured administrator already exists
- **THEN** bootstrap exits successfully without duplicating the administrator or changing its password

#### Scenario: Existing sole administrator uses a different username
- **WHEN** non-local bootstrap or bootstrap without the local adoption opt-in finds exactly one independent administrator whose username differs from the configured bootstrap username
- **THEN** it fails closed without creating a second administrator or changing credentials, and an operator can run an explicit sole-administrator adoption command that preserves the administrator ID and privileged history, changes the username and password, revokes existing administrator sessions, requires the next login to change the password, and appends an audit event

#### Scenario: Retained local Compose volume has one differently named administrator
- **WHEN** local Docker Compose starts with its explicit adoption opt-in and the database contains exactly one independent administrator under a different username
- **THEN** bootstrap preserves that administrator ID and privileged history, changes it to `admin` with the configured local password, revokes existing administrator sessions, requires the next login to change the password, appends an audit event, and subsequent starts preserve the adopted password

#### Scenario: Non-local deployment uses the local default password
- **WHEN** a non-local environment attempts bootstrap with `sk532359025`
- **THEN** bootstrap fails closed with a safe configuration error

### Requirement: Legacy shared administrator authority retirement
The migration SHALL revoke sessions that depended on `users.is_platform_admin`, remove that field from the normal-user authority model and database schema, and preserve every former administrator `users` row as an ordinary user with its existing wallet, workspaces, projects, and assets unchanged. No normal-user ID SHALL be converted into or equated with an administrator ID.

#### Scenario: Former shared administrator logs in as a user
- **WHEN** a former flagged user authenticates through normal-user login after migration
- **THEN** the account has ordinary user access only and cannot enter the administrator console

#### Scenario: Legacy privileged session is replayed
- **WHEN** a session issued under the old shared-identity authorization is presented after migration
- **THEN** it grants no administrator authority

### Requirement: Protection of the sole administrator
The system MUST prevent actions that would suspend the current sole administrator, revoke all usable administrator sessions without an explicit credential-recovery path, or remove the last active administrator.

#### Scenario: Administrator attempts self-suspension
- **WHEN** the sole administrator targets the current administrator account for suspension
- **THEN** the server rejects the action with a Chinese explanation and writes no account mutation

### Requirement: Dedicated administrator shell
The frontend SHALL render administrator workflows in a dedicated console shell with an independent administrator authentication gate, separated from the normal creator `AuthGate`, `WorkspaceGate`, and workspace state. It SHALL provide grouped navigation for dashboard, users, finance, content/assets, AI operations, platform configuration, audit, imports, and observability.

#### Scenario: Administrator enters the console
- **WHEN** a platform administrator opens the default administrator route
- **THEN** the dashboard and administrator navigation render without creator workflow controls

#### Scenario: Administrator opens a deep link
- **WHEN** an administrator opens a supported console section or resource URL directly
- **THEN** the console restores the requested section after authorization and preserves applicable filters in the URL

### Requirement: Operational dashboard
The console SHALL provide a server-generated dashboard with an explicit time window and generation timestamp covering users, manual recharge orders, ticket credits and consumption, AI task outcomes, support-review items, and actionable data-integrity exceptions.

#### Scenario: Administrator loads today's operations
- **WHEN** the administrator selects the current-day window
- **THEN** the dashboard returns consistent totals and exception counts for that bounded window with a generation timestamp

#### Scenario: Dashboard query requests an excessive range
- **WHEN** a request exceeds the configured maximum dashboard window
- **THEN** the server rejects or caps the range with a safe Chinese explanation rather than running an unbounded aggregate

### Requirement: Consistent administrator list behavior
Administrator list APIs SHALL support stable server-side pagination, deterministic ordering, domain-relevant filters, and bounded search without loading all platform rows into the browser.

#### Scenario: Records share the same creation time
- **WHEN** a page contains records with equal timestamps
- **THEN** ordering uses the integer record ID as a deterministic tie breaker and page traversal neither skips nor duplicates rows

#### Scenario: Unsupported filter is submitted
- **WHEN** the browser supplies an unknown or invalid filter value
- **THEN** the API rejects it before querying and returns a safe Chinese validation error

### Requirement: Bounded administrator exports
The system SHALL allow an administrator to export filtered user, manual recharge order, ticket ledger/usage, and AI task projections as CSV while enforcing the same authorization, masking, filters, row caps, and audit policy as interactive lists. User-authored bodies and binary assets MUST NOT be included.

#### Scenario: Administrator exports filtered orders
- **WHEN** an administrator exports completed manual recharge orders for a bounded date range
- **THEN** the CSV contains only the filtered safe order projection and the export action is audited

#### Scenario: Export would exceed the row cap
- **WHEN** an export matches more than the configured maximum rows
- **THEN** the server rejects it with instructions to narrow the filters and does not create a partial ambiguous export

### Requirement: Administrator action feedback
The console SHALL use Chinese labels, loading states, empty states, validation, confirmation dialogs, success results, and safe correlated errors for every administrator workflow, and MUST NOT use browser-native prompts to collect sensitive reasons or display one-time credentials.

#### Scenario: Administrator action fails
- **WHEN** an administrator mutation fails validation, conflicts with a state transition, or encounters a server error
- **THEN** the console preserves entered non-secret values and displays a safe Chinese message with a correlation ID when available

### Requirement: Fail-closed console availability
The administrator console SHALL fail closed when independent administrator authentication, database authority, audit persistence, or required administrator configuration is unavailable.

#### Scenario: Audit persistence is unavailable
- **WHEN** an action requires an audit event but the event cannot be committed
- **THEN** the mutation is rolled back and the console reports that the operation was not completed
