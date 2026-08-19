## 1. Reproduce and lock the release blockers

- [x] 1.1 Add an edge-contract regression test proving that current cloud roots such as auth, workspaces, wallet, admin, media, Playground, and AI tasks return backend JSON rather than the SPA document.
- [x] 1.2 Add a multi-instance configuration test that warms version A in one `ConfigurationService`, activates version B through another instance, and requires the first runtime reader to observe B for a new request.
- [x] 1.3 Add a policy-wiring test matrix covering registration grant, session lifetime/count, media URL lifetime, retention, stale holds, per-user AI concurrency, and feature flags.
- [x] 1.4 Add a reverse-proxy rate-limit test proving that distinct trusted client addresses receive distinct network fingerprints and an untrusted forwarding header is ignored.

## 2. Versioned cloud edge routing

- [x] 2.1 Change the production cloud frontend API base to same-origin `/api/v1` while keeping desktop and development backend discovery behavior intact.
- [x] 2.2 Replace feature-specific Nginx proxy locations with one `/api/v1/` proxy that strips the edge prefix and returns backend JSON for unknown API paths.
- [x] 2.3 Apply consistent proxy headers, correlation IDs, upload limits, buffering, and timeouts to the complete API subtree.
- [x] 2.4 Remove the backend host-port publication from the default production Compose topology and expose it only on the private service network or an explicit diagnostics profile.
- [x] 2.5 Configure Uvicorn trusted-proxy handling for the internal Nginx hop and make authentication/audit code use the normalized request client.
- [x] 2.6 Add a bounded rollout compatibility route for old cloud root requests, metrics for compatibility usage, and a removal switch that cannot affect desktop routes.
- [x] 2.7 Update frontend API tests, Nginx route tests, cloud error-protocol tests, and desktop adapter tests for the new edge namespace.
- [x] 2.8 Add security headers and no-store HTML behavior needed to prevent stale frontend bundles from outliving the API compatibility window.
- [x] 2.9 Move the script side-panel's previous-episode summary and next-episode hook helpers to scoped PostgreSQL reads/writes and persistent metered `prompt.polish` execution, including safe text-result polling and optimistic persistence tests.

## 3. Runtime policy authority and propagation

- [x] 3.1 Define a typed runtime policy snapshot and source-of-truth registry that classifies database business policy, immutable operation snapshots, and deployment infrastructure settings.
- [x] 3.2 Replace instance-local indefinite active-configuration reuse with an indexed active-version check and immutable payload cache keyed by version ID.
- [x] 3.3 Wire the same version-aware configuration resolver into gateway, authentication, media, maintenance, administration, and worker composition without relying on in-process invalidation.
- [x] 3.4 Resolve registration grant and registration policy from the active database version immediately before registration and record version/amount metadata in the initial ledger entry.
- [x] 3.5 Resolve session idle/absolute lifetime from active policy at issuance, persist the calculated expiries, and preserve existing session snapshots after activation changes.
- [x] 3.6 Enforce `max_sessions_per_user` transactionally at registration/login by locking the user session set and revoking the oldest eligible sessions with audit records.
- [x] 3.7 Keep AI model, parameter, metering, exchange-rate, and per-user concurrency values snapshotted at task creation while ensuring later tasks observe a newly activated version.
- [x] 3.8 Cap signed media access by the active `signed_media_url_seconds` policy and remove client authority to extend the server maximum.
- [x] 3.9 Resolve soft-delete retention and stale-hold thresholds once per maintenance run and include the applied configuration version in metrics and audit summaries.
- [x] 3.10 Remove global Celery worker process concurrency from editable database policy, expose observed deployment capacity read-only, and warn on documented planning mismatches.
- [x] 3.11 Make missing or incomplete active policy fail closed for registration and AI work without falling back to environment business defaults.
- [x] 3.12 Add PostgreSQL integration tests for cross-process activation visibility, session-limit concurrency, policy snapshots, maintenance thresholds, and media TTL enforcement.
- [x] 3.13 Update the Chinese configuration administration UI to distinguish dynamic business policy, immutable effects, and deployment-managed read-only settings.

## 4. Controlled beta registration

- [x] 4.1 Replace the registration boolean with the validated modes `disabled`, `invite_only`, explicitly unverified `open`, and reserved `verified_open`, rejecting verified-open activation until a verification provider is operational.
- [x] 4.2 Add an Alembic migration for `registration_invitations` with canonical phone, invitation hash, creator, expiry, status, consumption, timestamps, indexes, constraints, and RLS.
- [x] 4.3 Add transaction-local pre-auth invitation lookup context and RLS policies that expose only a matching invitation during registration while preserving platform-admin access.
- [x] 4.4 Implement cryptographically random invitation issuance, keyed hashing, one-time plaintext projection, expiry, revocation, and audit-safe lifecycle services.
- [x] 4.5 Add Chinese platform-admin invitation list/create/revoke APIs and UI with required reason, canonical phone, expiry, status, and no plaintext history.
- [x] 4.6 Add a safe public registration-policy endpoint that exposes only registration mode and verification availability.
- [x] 4.7 Extend registration input with an invitation code in invite-only mode and atomically validate phone match, lock/consume the invitation, and create user, wallet, grant, workspace, and session.
- [x] 4.8 Preserve the first-admin bootstrap path as an explicit audited operational exception without creating a general invitation bypass.
- [x] 4.9 Add invitation-specific rate limits, nondisclosing Chinese errors, hashed diagnostics, and audit events for mismatch, expiry, replay, revocation, and consumption.
- [x] 4.10 Update the Chinese login/registration screen to hide registration when disabled, require/explain invitations in invite-only mode, and support phone/password signup without verification claims in open mode.
- [x] 4.11 Add unit, PostgreSQL concurrency, API, CSRF, frontend, and RLS tests for invitation success, mismatch, rollback, replay, expiry, revocation, and unauthorized administration.
- [x] 4.12 Migrate legacy `registration_enabled=false` to disabled and `true` to invite-only, with an upgrade test proving no unrestricted unverified mode is created.
- [x] 4.13 Add a separate idempotent local Compose registration-policy bootstrap that defaults to `open`, while production defaults to `disabled` and administrator bootstrap remains isolated from user-domain creation.

## 5. Deployed-stack release verification

- [x] 5.1 Add deterministic provider and private-object-store test adapters guarded by an explicit test-only deployment mode that normal cloud startup rejects.
- [x] 5.2 Add an ephemeral Compose test profile containing PostgreSQL, Redis, role bootstrap, migrations, backend, worker, Nginx/static frontend, and isolated test data volumes.
- [x] 5.3 Implement a browser-origin smoke runner that validates response status and content type through `/api/v1`, including unknown API 404 behavior and SPA route separation.
- [x] 5.4 Exercise invitation registration, secure cookies, login, CSRF-protected mutation, session limits, workspace selection, wallet visibility, cross-user denial, and logout through the edge.
- [x] 5.5 Exercise media upload/access and a deterministic AI quote, hold, dispatch, provider result, media persistence, settlement, and history projection through the edge.
- [x] 5.6 Exercise normal-user admin denial, administrator invitation/configuration actions, cross-instance configuration activation, and old-task snapshot preservation.
- [x] 5.7 Add release-policy tests that fail when any editable database field lacks a demonstrated runtime consumer or is actually deployment-managed.
- [x] 5.8 Add a GitHub release-check workflow for migration-chain validation, backend suites, frontend logic/UI suites, lint/typecheck, production build, Compose/Nginx checks, deployed-stack smoke, and reconciliation.
- [x] 5.9 Generate a Chinese release evidence report with artifact revision, migration head, configuration version, edge checks, test totals, canary IDs, and reconciliation outcome without secrets or raw user content.
- [x] 5.10 Add a staging-only real private OSS and low-cost provider canary procedure to complement deterministic adapter tests.
- [x] 5.11 Add a guarded preflight/execute/finalize staging canary command that requires a recent distinct-resource fingerprint manifest and explicit paid-call confirmation, validates isolation and quote boundaries, reconciles billing, rolls database policy closed, and cannot finalize until both deployment emergency gates are closed.
- [x] 5.12 Add a compatibility-retirement evidence command that validates a bounded complete log window, monotonic start/end metrics, `/api/v1` supported-client inventory, zero legacy traffic, and sanitized output without changing the deployment switch.
- [x] 5.13 Persist the dedicated Nginx legacy access log outside the frontend container lifecycle and add Compose/route regression coverage so a compatibility observation window cannot silently lose traffic evidence during replacement.
- [x] 5.14 Make the release workflow's standalone Nginx syntax check independent of backend DNS availability while still running the production image entrypoint and generated compatibility configuration.
- [x] 5.15 Bind every staging canary phase and its local reconciliation runtime to admin-only live PostgreSQL, Redis, OSS Bucket, and provider-account fingerprints so a valid isolation manifest cannot authorize operations or evidence collection against a different deployment.
- [x] 5.16 Add a root Makefile with Chinese help for local startup, builds, tests, checks, migrations, Docker operations, existing desktop packaging scripts, and a non-paid side-effect-bounded release check.
- [x] 5.17 Add a host-path-independent local Compose override that uses Git-ignored environment values, distinct database roles, loopback-only exposure, Docker-managed volumes, and explicit production Compose selection.
- [x] 5.18 Publish local PostgreSQL on loopback port 15433, centralize Dockerfiles under `docker/` without changing build contexts, and add Compose regression coverage and connection documentation.
- [x] 5.19 Add a local-only, audited AI configuration bootstrap that enables the server-side text routes needed by the local browser workflow without changing production fail-closed defaults.

## 6. Migration, operations, and final gate

- [x] 6.1 Update cloud operations documentation with `/api/v1`, internal-only backend networking, trusted proxies, registration modes, invitation handling, configuration authority, and compatibility-window rollback.
- [x] 6.2 Add upgrade and rollback runbooks that keep registration/new AI work disabled, preserve invitation and immutable billing history, and reconcile tasks/holds before traffic changes.
- [x] 6.3 Run the full migration chain against a fresh PostgreSQL 16 database and an upgrade fixture containing an active legacy registration configuration.
- [x] 6.4 Run the complete backend, worker, frontend, desktop, edge, deployed-stack, RLS, billing, import, and reconciliation suites and record exact results.
- [x] 6.5 Verify `docker compose config`, Nginx syntax, shell syntax, frontend production output, `git diff --check`, and strict OpenSpec validation.
- [ ] 6.6 Keep registration and new AI tasks fail-closed until the evidence report passes, then open invitation-only registration before any broader rollout.
- [ ] 6.7 Remove the temporary unversioned edge compatibility route only after access metrics show no supported cloud client depends on it.
