## Context

LumenX cloud mode currently grants platform authority through `users.is_platform_admin` and reuses the normal user session, cookie, principal, and workspace-oriented frontend gate. That shared identity gives an operator user-domain semantics it does not need and makes the administrator boundary depend on a mutable flag in the front-office account table. The platform also has authenticated administrator APIs, user security actions, ticket adjustments, global task and usage queries, model configuration, audit events, and local-import operations, but the current administrator UI is a flat section list inside the main SPA and the backend has no recharge-order domain, per-user operational projection, or administrator workflow for system scene assets. Normal cloud repositories and PostgreSQL row-level security correctly assume user/workspace ownership, so administrator inspection must be explicit rather than implemented by weakening normal scope checks.

The first release has one trusted system super administrator. Recharge is recorded manually after the administrator confirms offline receipt; there is no payment gateway. Database identifiers must remain auto-incrementing integers, and migrations must not introduce foreign-key constraints. User-owned assets and media remain private, while system scene templates are platform-owned and read-only to normal users.

## Goals / Non-Goals

**Goals:**

- Deliver a dedicated administrator console shell and coherent operational information architecture.
- Physically separate administrator accounts, sessions, cookies, principals, and authorization from normal users.
- Bootstrap one independent administrator without creating a wallet, workspace, project, or user-owned asset.
- Give the administrator a user-centric view across account, workspace, content, assets, tasks, and finance without impersonating the user.
- Add an auditable and idempotent manual recharge-order lifecycle tied atomically to ticket wallets and immutable ledger history.
- Manage platform scene templates as system-scoped assets with private system media and safe user consumption.
- Preserve normal user/workspace isolation while adding narrowly defined, audited administrator inspection.
- Provide indexed queries, exception views, reconciliation, migration verification, and focused frontend/backend tests.

**Non-Goals:**

- External payment providers, callbacks, invoices, tax processing, or cash settlement automation.
- Approval workflows or fine-grained RBAC in the first release.
- Administrator impersonation, use of user-facing APIs to bypass ownership, or unrestricted asset downloads.
- Administrator editing or hard-deleting user-owned projects, scripts, assets, or media through the inspection console.
- Replacing the existing wallet, ledger, task, configuration, audit, content, or asset subsystems.
- Multiple administrator roles, shared user/administrator login, or automatic administrator provisioning from a normal user.

## Decisions

### 1. Introduce an independent administrator identity domain

Two independent tables will own administrator identity and session state:

- `admin_users`: auto-increment integer ID, unique canonical username, password hash, status, password-change timestamp, and lifecycle timestamps.
- `admin_sessions`: auto-increment integer ID, logical `admin_user_id`, hashed opaque session token, CSRF state, issuance/last-seen/expiry/revocation timestamps, and bounded client metadata.

No foreign keys are added; session-to-administrator relationships are validated by the service and covered by indexes, retention checks, and orphan verification. Administrator accounts do not create or reference a ticket wallet, workspace, project, or user-owned asset.

Administrator authentication lives under `/api/v1/admin/auth/*` and uses dedicated `lumenx_admin_session` and `lumenx_admin_csrf` cookies, a separate rate-limit namespace, and an independent session lifecycle. Normal user credentials, sessions, cookies, and CSRF tokens are never accepted by administrator endpoints, even when the same username or phone text exists in both domains. Sharing the application deployment and password-hashing implementation is acceptable; sharing identity records or session authority is not.

A separately deployed administrator application was rejected for this release because the physical security boundary is established by independent identities, sessions, route dependencies, and database context. Keeping one frontend and backend deployment avoids duplicate build and localization pipelines without weakening that boundary.

### 2. Use an administrator-only principal and route gate

Administrator authentication produces `AdminPrincipal` and `AdminContext` values that cannot be constructed from `UserPrincipal` or `UserContext`. Every administration endpoint uses one shared administrator-session dependency, and administrator services and repositories require `AdminContext`; UI hiding is never treated as authorization. The `/admin` frontend route bypasses the normal `AuthGate` and `WorkspaceGate` and instead uses an independent administrator gate and administrator API client behavior. Dangerous self-actions remain blocked where they could remove the only administrator's access.

`users.is_platform_admin` is removed from runtime authorization and from the user schema after migration. A new RBAC schema was rejected because the user explicitly selected one super-administrator role. APIs remain organized by domain so later permission checks can be inserted without changing their resource model.

### 3. Use explicit administrator inspection repositories

Administrator inspection will use dedicated read-only query services. Each request names a target user and, where relevant, a target workspace; repositories validate that relationship and apply both target predicates even though the administrator RLS context can see platform rows. Normal user repositories retain their current signatures and nondisclosing cross-user behavior.

PostgreSQL policies will permit the application role to read scoped rows only when a validated administrator request sets transaction-local `app.current_admin_id`. No `app.is_platform_admin` value derived from a normal user is accepted. The application role remains non-superuser and without `BYPASSRLS`; repositories still apply explicit target user/workspace predicates. Pooled connections use `SET LOCAL` and reset all context at transaction end.

### 4. Add a user operational projection, not a duplicated aggregate table

The user detail API will compose paginated projections from existing user, workspace, project, series, asset, media, AI task, usage, wallet, ledger, recharge-order, and audit records. The overview returns counts and recent exception summaries; individual tabs load on demand. No denormalized user-360 table will be maintained.

This avoids a second source of truth. Indexed on-demand queries are sufficient for the first-release operator volume; aggregate rollups can be added later if measured latency requires them.

### 5. Model manual recharge orders as a separate financial domain

Two new tables will be introduced:

- `manual_recharge_orders`: auto-increment integer ID, unique order number, target user, `CNY` cash amount in integer fen, ticket amount in integer microtickets, status, exchange/config snapshot, offline reference, administrator reasons, aggregate refunded amounts, optimistic version, administrator actor IDs, and timestamps.
- `manual_recharge_order_events`: auto-increment integer ID, order/user/administrator actor IDs, event type, idempotency key, amount deltas, linked ledger entry ID, reason, immutable snapshot, and timestamp.

There will be no database foreign keys. Required relationships are validated transactionally by services, supported by indexes and unique constraints, and checked by reconciliation. The generic audit log records who performed the action; order events are the domain history needed to reconstruct the financial state.

### 6. Enforce a transactional order state machine

The lifecycle is `pending -> completed -> partially_refunded -> refunded`; `pending -> cancelled` is also allowed. Completing an order locks the order and wallet, verifies the expected version, appends one recharge ledger credit, records one completion event, and updates both records in one transaction. Repeating an idempotency key returns the original result; reusing it with different data fails.

Refunds explicitly state cash fen and microtickets, cannot exceed the unrefunded order amounts, and cannot make the user's available wallet balance negative. Each accepted refund appends a debit ledger entry and immutable event rather than editing the original credit. Held tickets are not treated as available for refund. Gifts, compensation, and arbitrary debits remain administrator adjustments and never create paid orders.

### 7. Reuse system-scoped assets for the scene catalog

System scenes will use `assets` rows with `scope='system'` and `asset_type='scene'`. A validated payload schema will hold Chinese name/description, category, tags, generation prompt fields, visibility state, sort order, and provenance; the existing optimistic `version` protects edits. Archive uses `deleted_at`, while disablement removes a scene from new user selection without destroying references.

`media_objects` will gain a scope that supports private system media with null user/workspace ownership. System cover media is stored under a dedicated private object prefix and is readable through authorized short-lived access URLs. Users can browse enabled scenes and copy one into an owned project/workspace asset; that copy stores the source system asset ID and immutable source snapshot, so later catalog edits do not silently change existing work.

A separate scene-template table was rejected because the existing asset model already defines system scope and scene type.

### 8. Organize the administrator UI around operational jobs

Navigation groups will be:

- Dashboard
- Users: users and registration policy/invitations
- Finance: recharge orders, ticket adjustments, usage/ledger reconciliation
- Content and assets: user resource inspection and system scenes
- AI operations: tasks and support-review exceptions
- Platform: models/configuration, audit, imports, and observability

The user detail page will expose overview, workspaces, projects/series/scripts, characters/scenes/props/media, AI tasks, orders/wallet/ledger, and security/audit tabs. Lists use stable dimensions, server pagination, filters, empty/loading/error states, and Chinese terminology. Mutations use explicit dialogs rather than browser prompts.

### 9. Separate metadata inspection from sensitive previews

Ordinary administrator lists return masked phones, identifiers, counts, statuses, sizes, and safe summaries. Loading full script/prompt text or requesting a media preview requires a Chinese reason, emits an audit event containing administrator, target user/workspace/resource, correlation ID, and purpose, and returns only a bounded projection or short-lived inline URL. Raw object keys, provider credentials, hidden prompts, and unrestricted diagnostic payloads are never returned.

Inspection APIs are read-only. Existing domain-specific actions such as user suspension, recharge/refund, task cancellation, or system-scene mutation remain separate endpoints with their own state validation and audit events.

### 10. Make dashboard and exports bounded operational queries

Dashboard responses include a generated timestamp, requested time window, user/order/ticket/task totals, success/failure/support-review counts, and actionable exception counts. Queries use indexed time and status columns and enforce a bounded window. Initial implementation may cache nonfinancial dashboard aggregates briefly, but wallet/order detail is always read from authoritative rows.

CSV export is limited to administrator list projections for users, recharge orders, ledger/usage, and tasks; it uses the same filters, masking, authorization, row caps, and audit trail as the UI. User-authored bodies and binary assets are excluded.

### 11. Record privileged actors as administrators

New audit events, recharge orders/events, ticket adjustment correlations, configuration changes, system-scene mutations, exports, imports, and sensitive-access records store the independent administrator ID in an administrator-specific actor column. Existing user-actor columns are retained where needed to interpret historical rows, but new privileged operations write only the administrator actor field. Services enforce the allowed actor shape and reconciliation reports ambiguous or missing privileged actors.

This avoids rewriting immutable financial and audit history during migration. Reusing a user ID as the privileged actor was rejected because identical integer values across `users` and `admin_users` are unrelated identities.

### 12. Retire shared user authority without deleting user data

Migration revokes sessions that previously relied on `users.is_platform_admin`, removes the flag, and treats every existing `users` row as a normal front-office user. Any existing wallet, workspace, project, or asset owned by that user remains unchanged and accessible only through normal user authentication. No user-domain account is converted into or linked to the independent administrator.

After migrations, an idempotent Compose `admin-bootstrap` job creates `admin` in `admin_users` when absent using the configured initial password. The local Compose default is `sk532359025`; non-local deployments must override it through environment configuration and rotate it after first login. Re-running bootstrap never creates a normal user and does not silently overwrite an existing administrator password. Credential recovery uses an explicit administrator bootstrap/rotation operation, not the user password-reset path.

If a retained data volume already contains exactly one independent administrator under a different username, non-local bootstrap fails closed rather than creating a second record. Local Docker Compose explicitly opts into one-time sole-administrator adoption so `docker compose up` can preserve that administrator ID and every actor correlation while changing its username and password, revoking its existing administrator sessions, requiring a password change, and appending an audit event. The configured identity is idempotent on later starts and its password is not rotated again. The opt-in is absent from production/release composition, and both automatic and explicit recovery refuse multiple administrator rows.

## Risks / Trade-offs

- [Single super administrator has broad access] -> Centralize checks, require reasons for sensitive reads/mutations, mask by default, audit every privileged action, and keep the database role under RLS.
- [Default local administrator credentials could escape into a shared environment] -> Scope the built-in value to local Compose, require deployment override outside local mode, surface readiness failure for disallowed defaults, and require first-login rotation.
- [User and administrator cookies can be confused in one browser] -> Use distinct names and CSRF state, independent validation dependencies and rate-limit namespaces, and negative tests proving neither session works on the other API family.
- [Legacy user-admin sessions could retain authority] -> Revoke existing shared-identity sessions during migration, remove flag-based authorization, and test that former administrator users are ordinary users afterward.
- [Order and wallet state can diverge after partial failure] -> Use one database transaction for posting/refunds, unique idempotency constraints, immutable events, and explicit reconciliation reports.
- [No foreign keys allow orphan identifiers] -> Validate references under locks, add indexes/uniqueness/check constraints, test deletion/retention behavior, and report orphans without silently rewriting history.
- [Admin aggregate queries can become expensive] -> Load detail tabs lazily, require pagination and bounded windows, add query indexes, and measure before introducing rollup tables.
- [System media ownership changes touch shared RLS] -> Add fresh/legacy migration tests, explicit system/user policy cases, private-prefix checks, and rollback-compatible nullable columns.
- [Sensitive preview can expose user content] -> Separate preview permission path, require purpose, use short TTL inline URLs, omit raw keys, and log access.
- [Refunding spent tickets may be operationally inconvenient] -> Reject refunds that exceed available tickets and surface the exact shortfall; do not permit negative balances or silently reclaim held tickets.

## Migration Plan

1. Add `admin_users` and `admin_sessions` with integer identity IDs, checks, unique constraints, and indexes; do not add foreign keys. Add administrator-specific actor columns while retaining historical actor fields.
2. Add nullable/backward-compatible media scope fields and new recharge order/event tables with integer identity IDs, checks, unique constraints, and indexes; do not add foreign keys.
3. Backfill existing media rows as `user` scope, validate invariants, then enforce the new media scope check and RLS policies based on `app.current_admin_id`.
4. Extend ticket-ledger entry types and add nullable recharge correlation and administrator-actor columns/indexes; existing ledger and audit rows remain unchanged.
5. Deploy independent administrator authentication and administrator route dependencies, revoke legacy shared-identity administrator sessions, remove `users.is_platform_admin`, and verify former administrator users retain only ordinary user access.
6. Run the idempotent administrator bootstrap and verify that `admin` exists only in `admin_users` with no wallet, workspace, or assets. For local Compose, verify that a retained volume with one differently named independent administrator is automatically adopted with ID/history preserved and sessions revoked; non-local environments use the explicit adoption recovery. Deploy administrator domain APIs behind the administrator-console feature flag.
7. Seed or import initial system scenes idempotently, validating system media and asset payloads before enabling them.
8. Deploy the independent admin shell and run user/admin cross-session rejection, order, wallet, scene, task, sensitive-preview, RLS, migration, localization, and real-browser workflow tests.
9. Enable the console after reconciliation reports zero wallet/order mismatches, no orphan administrator sessions/actors, and no cross-user or system-media policy failures.

Rollback disables the new console and mutations first. Before privileged records are created, authentication can temporarily restore the previous deployment only if the legacy column and session semantics were preserved by the deployment plan; it must never reinterpret an `admin_users.id` as a `users.id`. Pending orders can be cancelled; completed/refunded financial rows, ledger entries, and administrator audit history are retained. After production privileged or financial events exist, forward fixes must preserve independent identity and immutable history.

## Open Questions

No blocking product decisions remain for the first release. External payment adapters, administrator RBAC, approval thresholds, invoice support, and richer content-moderation actions are intentionally deferred and require separate changes.
