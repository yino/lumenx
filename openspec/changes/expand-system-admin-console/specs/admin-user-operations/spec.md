## ADDED Requirements

### Requirement: Administrator user directory
The system SHALL allow the system administrator to search and filter normal users by integer ID, canonical phone, account status, registration period, and wallet exception state using masked list projections and deterministic pagination. The directory MUST NOT present or mutate administrator authority because administrator identities are not stored in `users`.

#### Scenario: Administrator searches by phone
- **WHEN** the administrator enters a complete or supported partial phone query
- **THEN** the directory returns matching users with masked phone, status, creation time, workspace count, task summary, and ticket balance

#### Scenario: Search does not match
- **WHEN** the query matches no user
- **THEN** the console renders a Chinese empty state without exposing nearby phone numbers or users

### Requirement: Atomic administrator user creation
The administrator-created front-office user workflow SHALL create a normal user, wallet, configured initial grant, and default workspace atomically, require an audit reason, and return the created safe user projection. It MUST NOT create an administrator identity or session.

#### Scenario: Administrator creates a user
- **WHEN** the administrator submits a valid unused phone, compliant initial password, and reason
- **THEN** exactly one active normal user, wallet, configured initial grant, and default workspace are committed, no `admin_users` row is created, and the action is audited under the independent administrator ID

#### Scenario: User creation conflicts
- **WHEN** the phone already exists or a dependent record cannot be created
- **THEN** the request fails without leaving a partial user, wallet, ledger entry, or workspace

### Requirement: Consolidated administrator user detail
The system SHALL provide a per-user administrator detail view with overview, workspaces, projects/series/scripts, characters/scenes/props/media, AI tasks, manual recharge orders, wallet/ledger/usage, security, and audit tabs backed by lazy paginated endpoints.

#### Scenario: Administrator opens a user
- **WHEN** the administrator selects a user from the directory
- **THEN** the overview shows authoritative account, balance, activity, content counts, recent exceptions, and links to each detail tab

#### Scenario: Detail tab contains many resources
- **WHEN** the selected user owns more resources than one page
- **THEN** the tab loads a deterministic bounded page and does not preload unrelated tabs or full content bodies

### Requirement: User security operations
The console SHALL support audited account suspension/reactivation, all-session revocation, and administrator-assisted password reset while preserving existing authentication security rules.

#### Scenario: User is suspended
- **WHEN** the administrator confirms suspension with a reason
- **THEN** the account becomes suspended, existing sessions become unusable, new AI work is prevented, and the action appears in the user's security history

#### Scenario: Reset credential is issued
- **WHEN** the administrator issues a reset credential with a reason
- **THEN** the credential is short-lived and single-use, is displayed once in a protected dialog, and is never returned by subsequent user-detail reads

### Requirement: User-centric operational filtering
All administrator task, usage, ledger, order, workspace, content, asset, media, and audit list APIs SHALL accept an explicit target user filter and SHALL verify any supplied workspace belongs to that user.

#### Scenario: Administrator filters tasks from user detail
- **WHEN** the user detail requests AI tasks for a selected workspace
- **THEN** the API returns only tasks matching both the target user and workspace

#### Scenario: Workspace belongs to a different user
- **WHEN** an administrator query combines one user ID with another user's workspace ID
- **THEN** the server rejects the invalid target scope and returns no resources

### Requirement: Administrator user-action audit
Every administrator mutation affecting a user SHALL require a nonempty Chinese reason and atomically append an audit event containing the independent administrator ID, target user, action, target record, correlation ID, and safe before/after state. A normal-user ID MUST NOT be recorded as the privileged actor for a new administrator action.

#### Scenario: Audit append fails
- **WHEN** the user mutation succeeds in memory but its required audit event cannot be persisted
- **THEN** the entire transaction rolls back and the user remains unchanged
