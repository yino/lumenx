## Why

The completed user/workspace/ticket platform establishes the right hosted-product architecture, but a second production-readiness review found release-blocking gaps between the declared contracts and the deployed behavior. In particular, the Docker frontend does not proxy most cloud APIs, activated platform configuration can remain stale or be ignored by runtime services, and public phone registration without SMS verification creates an account-claiming risk.

This change makes the existing scope suitable for a controlled beta before registration or billable AI work is enabled. It does not add organizations, online payment, or SMS delivery.

## What Changes

- **BREAKING** Give hosted APIs one explicit, versioned same-origin namespace, `/api/v1`, and proxy that namespace as a whole instead of maintaining an incomplete list of root paths. Keep desktop compatibility through its adapter and legacy-route boundary.
- Forward and trust client-network metadata only across the known reverse-proxy boundary so authentication rate limits do not collapse all users into the Nginx container address and cannot be spoofed by direct clients.
- Make active database configuration authoritative for every business policy exposed in the administration UI, including registration grant, session lifetime and count, per-user AI concurrency, feature flags, media URL lifetime, retention, and stale-hold handling.
- Remove infrastructure capacity such as Celery worker process concurrency from mutable product configuration, or expose it as read-only deployment state with a parity warning. It remains controlled by Compose/environment and requires a rollout to change.
- Replace instance-local indefinite active-configuration caching with version-aware, cross-process-safe refresh semantics so a newly activated version affects new requests without restarting backend or worker processes, while in-flight tasks retain their snapshots.
- Introduce controlled registration modes: `disabled`, `invite_only`, and a reserved future `verified_open`. Until SMS verification exists, production activation cannot enable unrestricted phone registration. Add audited, expiring, single-use, phone-bound invitations for the beta.
- Add deployed-stack acceptance checks that exercise the browser-facing Nginx origin through registration/login, workspace selection, wallet access, media upload/access, administration authorization, AI quote/task submission with a fake provider, and logout.
- Add release gates that fail on missing proxy coverage, ignored database settings, stale configuration after activation, untrusted forwarded addresses, or an unsafe registration mode.
- Turn the two remaining operational judgments into guarded evidence workflows: a three-phase real staging OSS/provider canary that cannot pass until all entry gates are closed again, and a compatibility-retirement analyzer that requires a complete observation window, supported-client inventory, and zero legacy traffic.

## Capabilities

### New Capabilities

- `cloud-api-edge-routing`: Versioned same-origin API routing, trusted proxy metadata, upload limits, and cloud/desktop route boundaries.
- `runtime-policy-consistency`: Database-backed business policy authority, version-aware refresh, immutable task/session snapshots, and deployment-managed infrastructure settings.
- `controlled-beta-registration`: Audited invitation-only phone registration while SMS ownership verification remains unavailable.
- `cloud-release-verification`: Automated deployed-stack, security, configuration-propagation, and rollback-gate acceptance criteria.

### Modified Capabilities

None. The predecessor change has not yet been archived into `openspec/specs`, so this follow-up introduces distinct hardening capabilities rather than declaring deltas against unavailable main specs.

## Impact

- Backend API composition, route prefixes, authentication network-source handling, session creation, registration, feature gates, configuration loading, maintenance policy resolution, media signing, and worker runtime configuration.
- Frontend API base URL and cloud request routing; desktop behavior remains supported.
- Nginx, Docker Compose, health checks, trusted-proxy settings, and release configuration.
- PostgreSQL migrations for phone-bound invitations and any configuration schema cleanup; existing user, task, usage, wallet, ledger, and audit history remain intact.
- Test and operations coverage, including a real Nginx/frontend/backend/PostgreSQL/Redis stack and deterministic fake-provider execution.
- Staging and rollout evidence tooling. The tools do not autonomously call a paid provider, open registration, or remove compatibility routing without explicit operator confirmation and external evidence.
