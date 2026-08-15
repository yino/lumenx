## Context

The predecessor change added cloud authentication, user/workspace isolation, ticket accounting, database model configuration, a persistent AI gateway, and Chinese administration UI. Its component and PostgreSQL tests are broad, but the production topology was not exercised from the browser-facing Nginx origin.

The current Docker frontend computes its cloud API base as the page origin. Nginx proxies a hand-maintained list of legacy roots and omits most new cloud roots, including authentication, workspaces, wallet, administration, media, Playground, and AI task routes. Requests to those paths fall through to `index.html`, so a healthy frontend and backend can both return HTTP 200 while the hosted workflow is unusable.

The configuration schema also mixes three kinds of settings: dynamic business policy, immutable task/session snapshots, and deployment capacity. Several database fields are displayed as editable but runtime authentication and maintenance services still read environment defaults. Active configuration is cached per `ConfigurationService` instance, so activation through the administration instance does not invalidate the AI gateway instance or other processes.

Finally, a phone number without SMS verification is only a login identifier. Unrestricted registration would let the first caller claim another person's number. The release must therefore remain controlled until ownership verification exists.

## Goals / Non-Goals

**Goals:**

- Make every browser-facing cloud API reachable through one versioned same-origin route.
- Ensure proxy-derived client identity is accurate enough for rate limiting and cannot be forged through a public backend port.
- Define and enforce one source of truth for every editable platform setting.
- Propagate newly activated configuration to new requests across backend and worker processes without restarts.
- Preserve immutable configuration snapshots for already-created sessions and AI tasks.
- Support invitation-only beta registration without implementing SMS.
- Make a real container-topology test, not component test counts, the cloud release gate.

**Non-Goals:**

- Organization tenants, memberships, shared workspaces, or invitations to collaborate.
- SMS delivery, phone ownership verification, OAuth, MFA, or public password recovery.
- Public unverified registration, online payment, subscriptions, or invoicing.
- Dynamic resizing of Celery processes from the administration UI.
- Replacing PostgreSQL, Redis, OSS, Nginx, Celery, or the existing desktop adapters.
- Renaming existing database business tables or rewriting immutable ticket history.

## Decisions

### 1. The edge owns a single `/api/v1` namespace

Cloud frontend requests use `${window.location.origin}/api/v1`. Nginx proxies the complete `/api/v1/` subtree to the backend and strips the edge prefix. The backend's container port is internal-only in the default Compose topology; `/health` and `/ready` remain available through `/api/v1/health` and `/api/v1/ready` at the edge.

This keeps current backend route implementations and desktop root routes stable while giving the hosted product one unambiguous browser contract. Nginx no longer needs one `location` per feature. The SPA fallback handles only non-API paths, and any unknown `/api/v1` path returns backend JSON 404 rather than `index.html`.

Alternative considered: add every missing root path to Nginx. Rejected because every new capability can silently reintroduce the same omission.

Alternative considered: immediately prefix every FastAPI route internally. Deferred because it creates unnecessary collisions with the desktop API surface. The edge namespace is the externally supported cloud contract; a later API-only deployment can mount the same routers under a backend prefix.

### 2. The reverse proxy is the only public network hop

Nginx forwards `Host`, `X-Forwarded-For`, `X-Forwarded-Proto`, and a correlation identifier. Uvicorn trusts forwarded metadata only in a topology where its port is not publicly published, or from an explicitly configured trusted-proxy CIDR in non-Compose deployments. Authentication and audit code consume the normalized request client produced by this trusted proxy boundary rather than parsing arbitrary headers itself.

The generic API location applies upload limits and timeouts suitable for media and long-polling metadata calls. Provider execution remains asynchronous, so the edge does not hold provider-generation requests open.

Alternative considered: trust `X-Real-IP` in application code. Rejected because direct clients could spoof it unless every deployment reproduced an application-level trusted-proxy list.

### 3. Settings are classified by authority and activation semantics

The platform uses the following source-of-truth matrix:

| Class | Examples | Authority | Activation semantics |
| --- | --- | --- | --- |
| Business policy | ticket rate, registration mode/grant, session lifetime/count, per-user AI concurrency, signed-media TTL, retention, stale holds | active PostgreSQL config version | applies to new operations after commit |
| Immutable snapshot | task model/formula/rate, session expiry policy, invitation policy used by registration | task/session/ledger records | never changes retroactively |
| Deployment infrastructure | database/Redis/OSS endpoints, secrets, Celery process concurrency, trusted proxies | environment/Compose/secret manager | restart or rollout required |

Infrastructure capacity is removed from editable configuration. The administration surface may show observed worker capacity as read-only and warn when it differs from the capacity used by load planning, but activation cannot claim to resize workers.

Registration reads the active policy immediately before its transaction and records the configuration version and grant in ledger metadata. Session creation snapshots idle and absolute expiry, enforces the active session-count limit under a user-scoped lock, and revokes the oldest eligible sessions when necessary. Existing sessions keep their stored expiries.

Media signing caps server-issued URLs at the active TTL. Cleanup and stale-hold jobs resolve the active retention policy at each run and record the policy version in their audit summary.

### 4. Active configuration refresh is version-aware, not instance-invalidated

Every new runtime operation queries the small active-version identity from PostgreSQL. A process may cache the immutable payload keyed by that version ID, but it must not reuse a payload without confirming that the active ID is unchanged. After an activation transaction commits, the next new operation in every backend or worker process observes the new ID without Redis pub/sub or restart.

AI tasks retain the selected model, formula, allowed parameters, and exchange rate in their existing snapshot. Sessions retain calculated expiry timestamps. Activation therefore changes future operations only.

Alternative considered: Redis invalidation messages. Rejected for the first release because message loss and subscriber lifecycle add complexity; a cheap indexed active-ID lookup provides deterministic consistency.

Alternative considered: a shared in-process singleton. Rejected because it does not solve multiple backend containers or Celery workers.

### 5. Registration is an explicit mode, not a boolean

The active platform policy defines `disabled`, `invite_only`, or reserved `verified_open`. Activation rejects `verified_open` until a verification provider declares send/verify capability available. There is no `open_unverified` mode.

In `invite_only`, a platform administrator creates an expiring, single-use invitation bound to one canonical phone. Only a hash of the random invitation secret is stored. Registration normalizes the submitted phone, validates the invitation, creates the user/workspace/wallet/session, and consumes the invitation in one database transaction. A failed registration does not consume it. Bootstrap of the first platform administrator remains an explicit operational exception.

Existing boolean feature flags migrate fail-closed: `false` becomes `disabled`; `true` becomes `invite_only`. Operators must create invitations before registering beta users. Invitation creation, revocation, expiry, consumption, and failed mismatch attempts are audited without storing the plaintext code.

### 6. Release evidence comes from the deployed topology

A dedicated test profile starts PostgreSQL, Redis, migrations, backend, worker, Nginx/static frontend, and deterministic test adapters for provider and object storage. Test-only adapters require an explicit test environment and fail startup in normal cloud mode.

Acceptance requests enter through the Nginx origin and cover JSON content types, cookies, CSRF, user/workspace isolation, wallet, media references, administration denial, AI reservation/dispatch/settlement, logout, and unknown API paths. A configuration test uses separate service/process instances to prove post-activation propagation while an existing task retains its snapshot.

CI runs schema migration, backend tests, frontend logic/UI tests, type checking, production build, Compose validation, Nginx route coverage, and the deployed-stack smoke test. Build configuration may continue to decouple static export from linting, but the release job cannot pass unless lint/type/test stages pass independently.

### 7. External release evidence is guarded and repeatable

The real staging canary has `preflight`, `execute`, and `finalize` phases. Preflight requires a non-production staging identity, HTTPS, private OSS, production provider adapters, and registration/AI closed in both database and deployment policy. A recently reviewed manifest must prove that PostgreSQL, Redis, OSS Bucket, and provider-account fingerprints differ from production without recording the raw resource identifiers. An admin-only deployment-state projection returns SHA-256 fingerprints of the current non-secret resource identities, and every canary phase requires the manifest's staging values to match that live projection. The command's local runtime fingerprints must also match the edge projection before direct PostgreSQL reconciliation is allowed. PostgreSQL and Redis use deployment-managed stable cloud-instance or host/volume inventory identifiers instead of ambiguous Compose service aliases; the provider fingerprint similarly uses a stable account/subaccount identifier associated with the actual credentials. Normal cloud startup remains backward compatible when these identifiers are absent, but a real canary fails closed. Execute additionally requires an exact isolated-environment confirmation, an explicit one-paid-call confirmation, a reviewed provider model, a maximum quote, two existing isolated canary users, and a known closed configuration version. Registration remains closed throughout. The tool runs one media and provider workflow, reconciles billing through PostgreSQL, and always attempts to roll database policy back to the known closed version. Finalize cannot pass until operators restore both deployment emergency gates and reconciliation remains consistent.

Compatibility removal similarly requires a minimum observation window, complete dedicated Nginx legacy logs persisted outside the frontend container lifecycle, start/end metric snapshots without resets or increments, and a supported-cloud-client inventory in which every client has validated `/api/v1`. Evidence contains only hashes and aggregate counts. The analyzer can authorize a later deployment change but cannot edit `LUMENX_LEGACY_API_COMPAT` itself.

### 8. Repository commands are explicit and side-effect bounded

The repository root provides a Makefile as the discoverable entry point for local setup, split or combined development servers, frontend production builds, backend/frontend tests, static checks, Alembic migration, production Compose operations, and the existing macOS/Windows packaging scripts. The Makefile delegates to existing project scripts and package commands instead of duplicating their platform-specific behavior.

The default target is Chinese help. Local development does not implicitly start the production Compose topology because that topology requires deployment secrets and fail-closed cloud policy. Docker startup validates Compose configuration first. The local `release-check` target combines tests, static checks, build, Compose validation, and strict OpenSpec validation only; it cannot execute a real staging provider canary, confirm a paid call, enable registration or AI work, or retire compatibility routing.

### 9. Local Docker is host-path independent

Default Docker Compose discovery merges a repository local override that injects local-only credentials from the Git-ignored `.env`, resets file-backed production secrets, binds the public edge only to loopback, and replaces host `output` and `imports` bind mounts with Docker-managed named volumes. A setup command generates distinct random PostgreSQL administrator/application passwords and a session HMAC secret, then maps existing OSS and provider credentials without printing them. Application services retain the non-superuser database role so local startup does not bypass RLS.

Production and release commands select `docker-compose.yml` explicitly and therefore continue to use file-backed or deployment-managed secrets. The local override does not weaken production credential handling, enable registration or AI gates, or introduce a host filesystem dependency merely because the repository lives outside Docker Desktop's default shared directories.

The local override publishes PostgreSQL as `127.0.0.1:15433:5432` for host-side database tools; the production Compose service remains private and has no `ports` declaration. Backend, frontend, and PostgreSQL utility Dockerfiles live below `docker/`, while every Compose build keeps the repository root as its context so existing source and configuration `COPY` paths remain stable.

### Product decision review

PM recommendation: **proceed with a controlled beta, with commercial caveats**.

- **Customer outcome:** independent creators gain a hosted workflow whose projects, workspaces, assets, media, and AI history remain private per user while model choice and credentials stay server-managed.
- **Business outcome:** establish auditable cost attribution and a safe invite-only beta before pursuing self-service growth. This release is a reliability and control investment, not yet a validated revenue launch.
- **Solution hypothesis:** if hosted execution combines user-scoped data, immutable metering, server-side AI routing, and visible Chinese workflows, creators can move from a local tool to a managed service without losing creative continuity or billing trust.
- **Pricing decision:** `tokens_per_ticket` remains a versioned consumption conversion used for reservations and settlement. A ticket is not yet a priced SKU. Without ARPU, willingness-to-pay, conversion, churn, provider unit-cost, and support-cost baselines, the system must not infer a retail price, sell top-ups, or claim target margin.
- **Language boundary:** human-visible cloud product copy is Chinese. Stable API codes, model IDs, protocol names, and operator-only technical identifiers remain machine-readable and are not treated as untranslated page copy.

Act immediately: prove real provider/OSS settlement, keep registration invite-only, reconcile every canary, and retain fail-closed rollback. Start tracking: task success and latency, token-to-cost variance by model, support-review rate, invitation activation, creator retention, willingness to pay, gross margin, AI/content regulation, and provider dependency concentration. Public registration, recovery, payment, subscriptions, and ticket pricing each require separate evidence and scope.

## Risks / Trade-offs

- [Changing the cloud API base breaks cached frontend assets] -> Deploy Nginx support for `/api/v1` before or atomically with the new static build, use no-store HTML, and keep a short compatibility proxy only during the rollout window.
- [Trusting all forwarded addresses exposes spoofing] -> Remove the backend host port in the default topology and require explicit trusted-proxy configuration elsewhere.
- [An active-ID database lookup adds traffic] -> Use one indexed scalar query per new operation and cache immutable payloads by version ID; measure latency before adding a distributed cache.
- [Invitation support adds another credential] -> Store only a keyed hash, show plaintext once, set short expiry, rate-limit attempts, and audit lifecycle events.
- [Revoking oldest sessions may surprise users] -> Return the enforced limit in session-management responses and show revoked/active devices in the Chinese account UI.
- [Fail-closed migration can block previously open registration] -> Document the behavior, pre-create beta invitations, and verify the mode before traffic cutover.
- [Test adapters diverge from OSS/provider behavior] -> Keep adapter contract tests against production interfaces and run a separate staging canary with real private OSS and one low-cost provider task.

## Migration Plan

1. Add invitation storage and configuration schema migration. Translate legacy registration flags to `disabled` or `invite_only`; do not enable registration.
2. Implement version-aware policy resolution and wire business-policy consumers while feature gates remain closed.
3. Add `/api/v1` frontend base and generic Nginx proxy. Deploy the edge compatibility window before switching cached frontend assets.
4. Remove the backend public port from the default production topology, configure trusted proxy handling, and verify rate-limit fingerprints from two client addresses.
5. Run the deployed-stack suite, real OSS staging canary, migration check, wallet reconciliation, and task/hold reconciliation.
6. Create phone-bound invitations for beta users, activate `invite_only`, and keep AI new-task creation limited to canary users until settlement is verified.
7. Remove temporary unversioned edge proxies after access logs show no supported clients using them.

Rollback keeps both registration and new AI work fail-closed, restores the previous static bundle and compatibility proxy, drains or reconciles persisted tasks, and never downgrades or deletes invitation, ledger, usage, task, configuration, or audit history. A faulty active configuration is replaced by a newly activated version rather than edited in place.

## Open Questions

No blocking product question remains for a controlled beta. Public self-service registration remains blocked until SMS verification and the corresponding account-recovery policy are proposed separately.
