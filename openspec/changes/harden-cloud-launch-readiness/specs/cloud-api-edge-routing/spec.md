## ADDED Requirements

### Requirement: Versioned same-origin cloud API
The hosted product MUST expose every supported browser API under the single same-origin prefix `/api/v1`, while frontend navigation and static assets MUST remain outside that prefix.

#### Scenario: Browser requests current user
- **WHEN** the cloud frontend requests `/api/v1/auth/me`
- **THEN** Nginx forwards the request to the backend and returns the backend JSON response rather than the SPA document

#### Scenario: Unknown API route is requested
- **WHEN** a client requests an unknown path below `/api/v1`
- **THEN** the response is a backend JSON 404 and MUST NOT contain `index.html`

### Requirement: Complete edge proxy coverage
The edge proxy SHALL forward the entire versioned API subtree without maintaining feature-specific location lists, including authentication, workspaces, wallet, administration, AI tasks, media, projects, series, assets, Playground, health, and readiness routes.

#### Scenario: New API root is added
- **WHEN** a backend route is added below the supported internal API surface
- **THEN** it is reachable through `/api/v1` without adding another Nginx location block

### Requirement: Cloud content helpers use the hosted execution boundary
Browser content helpers that combine persisted project state with AI generation MUST read and write user-scoped PostgreSQL documents, and MUST submit generation through the persistent metered AI gateway instead of invoking the desktop pipeline or a provider directly.

#### Scenario: User opens the next-episode hook panel
- **WHEN** an authenticated user reads a project's next-episode hook in the selected workspace
- **THEN** the server returns the PostgreSQL-backed cache, staleness state, and optimistic version without creating an AI task

#### Scenario: User generates a next-episode hook
- **WHEN** an authenticated user explicitly requests generation for a non-empty owned project
- **THEN** the server builds the prompt from the stored script ending, submits a `prompt.polish` persistent metered task, exposes only the safe text result to that task owner, and persists the accepted result with optimistic version control

### Requirement: Internal-only backend boundary
The default cloud Compose topology MUST make Nginx the public application entry point and MUST NOT publish the backend service port to external interfaces.

#### Scenario: Client bypasses Nginx
- **WHEN** an external client attempts to connect directly to the backend container port in the default production topology
- **THEN** the connection is unavailable while Nginx can still reach the backend over the private service network

### Requirement: Trusted proxy client identity
The cloud deployment MUST derive client network identity from forwarded metadata only when the immediate peer is a configured trusted proxy, and authentication rate limiting and audit fingerprints MUST use that normalized identity.

#### Scenario: Two clients register through Nginx
- **WHEN** two distinct client addresses send registration attempts through the trusted proxy
- **THEN** network rate limits maintain distinct fingerprints rather than treating both clients as the Nginx container

#### Scenario: Direct client spoofs forwarding header
- **WHEN** an untrusted peer submits an `X-Forwarded-For` value
- **THEN** the application ignores the spoofed value and uses the immediate peer identity

### Requirement: Uniform proxy request policy
The versioned API proxy MUST apply required host, protocol, client-address, correlation, upload-size, and timeout behavior consistently across all cloud API routes.

#### Scenario: User uploads project media
- **WHEN** an authorized user uploads an allowed media object through `/api/v1/media`
- **THEN** the edge accepts the configured maximum size and forwards the request metadata and body without falling back to the SPA

### Requirement: Desktop compatibility
Desktop mode SHALL retain its local adapter behavior and MUST NOT require the hosted `/api/v1` edge topology.

#### Scenario: Desktop application starts
- **WHEN** LumenX starts in desktop deployment mode
- **THEN** local root API routes, local media delivery, and desktop authentication adapters continue to work without Nginx

### Requirement: Evidence-gated compatibility retirement
The temporary unversioned hosted compatibility route MUST remain enabled until a complete bounded observation window proves zero legacy requests and every supported cloud client has validated the `/api/v1` contract. Dedicated compatibility access logs MUST survive frontend container replacement for the entire observation window.

#### Scenario: Frontend container is replaced during observation
- **WHEN** the frontend/Nginx container is recreated while the compatibility window remains active
- **THEN** the dedicated legacy access log remains available from deployment-managed persistent storage and the observation window does not silently lose evidence

#### Scenario: Legacy counter resets during the observation window
- **WHEN** start/end compatibility metric evidence decreases or logs are not attested as complete for the window
- **THEN** compatibility removal is rejected even if the latest counter value is zero

#### Scenario: Retirement evidence passes
- **WHEN** the minimum observation window has complete legacy logs, zero legacy requests, zero metric increment, and a validated supported-client inventory
- **THEN** the evidence authorizes a separate controlled deployment to disable compatibility without changing desktop routes
