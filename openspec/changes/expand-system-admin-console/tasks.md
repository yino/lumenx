## 1. Baseline and contracts

- [x] 1.1 Inventory the existing administrator routes, service dependencies, frontend sections, API client types, and tests; document which components are extended versus replaced.
- [x] 1.2 Define shared constants and typed schemas for admin navigation groups, bounded date windows, pagination limits, export caps, order states/events, and system-scene payloads.
- [x] 1.3 Replace shared-user `require_platform_admin` with one independent administrator-session dependency and apply it to all `/api/v1/admin/*` routers without weakening service-level authorization.
- [x] 1.4 Replace administrator `UserContext` usage with independent `AdminPrincipal` and `AdminContext` guards that reject missing administrator context before database access.
- [x] 1.5 Add contract tests proving normal-user sessions/cookies cannot load administrator APIs, administrator sessions cannot satisfy user/workspace APIs, and denied responses disclose no target-user or platform data.
- [x] 1.6 Add feature-flag/configuration wiring that can disable the expanded console and its mutations while retaining existing emergency operations.

## 2. Database models and migrations

- [x] 2.1 Add SQLAlchemy models for `manual_recharge_orders` and immutable `manual_recharge_order_events` using auto-increment integer primary keys and no foreign-key declarations.
- [x] 2.2 Add order check constraints, unique order number/idempotency constraints, optimistic version fields, cash-fen/microticket amount constraints, and indexes for user, status, reference, actor, and timestamps.
- [x] 2.3 Extend ticket-ledger entry types and add indexed nullable recharge order/event correlation IDs without foreign-key constraints.
- [x] 2.4 Extend media metadata with explicit `user` and `system` scopes, nullable owner fields for system media, and database checks that enforce valid scope/owner combinations.
- [x] 2.5 Add indexes and validated payload/lifecycle conventions required for system-scoped scene assets and their media.
- [x] 2.6 Create the next Alembic migration for orders, events, ledger correlations, media scope/backfill, indexes, checks, grants, and RLS policies.
- [x] 2.7 Replace shared-user administrator RLS flags with policies based on transaction-local `app.current_admin_id`, preserve normal-user policies, and keep the application role non-superuser without `BYPASSRLS`.
- [x] 2.8 Update PostgreSQL migration verification to assert integer identity IDs, zero foreign keys, current single head, required indexes/checks, immutable-event protections, and expected RLS policy behavior.
- [ ] 2.9 Verify both fresh-database and legacy-upgrade paths, including media-scope backfill, then document the pre-financial-data downgrade boundary and forward-fix rule.

## 3. Manual recharge order domain

- [x] 3.1 Implement typed order command/result models, safe projections, order-number generation, request fingerprints, and Chinese state labels.
- [x] 3.2 Implement an indexed manual-order repository with row locking, optimistic version checks, deterministic pagination, and filters by order/user/status/operator/reference/date.
- [x] 3.3 Implement pending-order creation with target-user validation, positive integer amounts, `CNY` cash snapshot, offline reference, reason, immutable create event, and audit event.
- [x] 3.4 Implement atomic pending-order completion that locks the order and wallet, appends exactly one recharge ledger credit/event/audit record, and updates lifetime totals.
- [x] 3.5 Implement pending-order cancellation with state validation, required reason, immutable event, and no wallet mutation.
- [x] 3.6 Implement partial and full refunds with cumulative cash/ticket caps, available-balance enforcement, wallet locking, refund ledger debit, state transition, event, and audit record.
- [x] 3.7 Implement administrator-scoped idempotency for create, complete, cancel, and refund commands, including fingerprint-conflict errors.
- [x] 3.8 Keep grants, compensation, and arbitrary debits on the existing adjustment path and explicitly exclude them from paid-order/revenue queries.
- [x] 3.9 Extend user wallet history projections with safe manual-recharge and recharge-refund entries while hiding administrator-only offline evidence.
- [x] 3.10 Add unit tests for every valid and invalid state transition, integer amount boundary, stale version, duplicate reference, and audit rollback.
- [x] 3.11 Add transactional/concurrency tests proving duplicate completion/refund retries cannot double-credit/debit and refunds cannot consume held tickets or make balance negative.

## 4. Manual recharge order APIs and reconciliation

- [x] 4.1 Add administrator endpoints to create, retrieve, list, complete, cancel, and refund manual recharge orders using typed request/response schemas.
- [x] 4.2 Require CSRF proof, administrator authorization, idempotency key, expected version, and Chinese reason on every order mutation.
- [x] 4.3 Return safe Chinese validation/conflict errors with correlation IDs and never imply external payment-provider verification.
- [x] 4.4 Implement immutable order-event timeline and ledger-correlation endpoints for order detail.
- [x] 4.5 Implement a non-mutating order reconciliation service that verifies state, events, ledger credits/debits, aggregate refunds, and wallet deltas.
- [x] 4.6 Add reconciliation report persistence/exception projection without silently repairing financial history.
- [x] 4.7 Add API tests for authorization, pagination/filtering, idempotency, stale versions, audit requirements, transaction rollback, and reconciliation mismatches.

## 5. System scene catalog backend

- [x] 5.1 Define and validate the versioned system-scene payload schema for Chinese name/description, category, tags, prompt fields, style/aspect metadata, visibility, sort order, and provenance.
- [x] 5.2 Implement administrator repositories/services to create, retrieve, list, update, enable, disable, reorder, and soft-archive `scope=system`/`asset_type=scene` assets.
- [x] 5.3 Enforce optimistic version conflicts and append safe before/after audit metadata for every scene mutation.
- [x] 5.4 Implement private system-media upload/storage under a dedicated object prefix with MIME/size/checksum validation and lifecycle cleanup.
- [x] 5.5 Implement administrator and enabled-user system-media access through short-lived URLs/streams without returning raw object keys.
- [x] 5.6 Add a read-only enabled system-scene catalog endpoint for authenticated users with deterministic category/sort ordering.
- [x] 5.7 Implement copying an enabled system scene into an owned project/workspace asset with source ID, source version, immutable snapshot, and media provenance.
- [x] 5.8 Compute scene usage/reference counts and require explicit confirmation metadata before disabling or archiving referenced scenes.
- [x] 5.9 Add an idempotent initial system-scene seed/import path that validates payloads/media and does not overwrite administrator changes.
- [x] 5.10 Add service/API/RLS tests for system ownership, normal-user read-only access, invalid media/payloads, stale edits, copied snapshots, and archive/reference preservation.

## 6. Administrator resource inspection backend

- [x] 6.1 Implement dedicated read-only administrator inspection repositories that require explicit target user and verified target workspace predicates.
- [x] 6.2 Add a user operational-overview query for account, wallet, workspace/content/asset/task/order counts, recent activity, and exception summaries without a duplicate aggregate table.
- [x] 6.3 Add deterministic paginated inspection queries for workspaces, projects, series/scripts, characters, scenes, props, media, AI tasks/attempts, usage, ledger, orders, security, and audit history.
- [x] 6.4 Define strict response allowlists that mask phones and omit full content bodies, raw object keys, secrets, hidden prompts, unrestricted diagnostics, and provider credentials.
- [x] 6.5 Add purpose-bound endpoints for bounded full script/prompt views and restricted task diagnostics that reauthorize target scope and atomically audit the sensitive read.
- [x] 6.6 Add purpose-bound private media preview endpoints with short-lived inline access, deletion/quarantine checks, and no unrestricted download route.
- [x] 6.7 Add detailed administrator AI-task projections covering ownership, attempts, state, model/config/metering snapshots, provider acknowledgement, holds, usage, settlement, result media, and safe errors.
- [x] 6.8 Add deduplicated operational exception queries for failed/stalled/support-review tasks, stale holds, order/ledger mismatches, missing media, and declared integrity failures.
- [x] 6.9 Add audit-search filters for actor, target user/workspace/resource, action, purpose, correlation ID, and bounded time range.
- [x] 6.10 Add tests proving inspection is read-only, user/workspace mismatches fail, missing purposes return no sensitive data, audit failure blocks previews, and pooled transactions do not leak administrator context.

## 7. Dashboard, list, and export APIs

- [x] 7.1 Implement a bounded administrator dashboard query with generation timestamp, requested window, user/order/ticket/task totals, support-review counts, and actionable exceptions.
- [x] 7.2 Add required database indexes and query-plan tests for dashboard, user detail, order, task, ledger, asset, and audit filters.
- [x] 7.3 Extend existing user, task, usage, audit, and import list APIs with deterministic ordering and validated user/workspace/date/status filters needed by the new console.
- [x] 7.4 Implement bounded CSV exports for safe user, order, ledger/usage, and AI-task projections using the same filters, masking, authorization, and row caps as list APIs.
- [x] 7.5 Audit every export and reject over-cap exports with a Chinese filter-narrowing response; exclude user-authored bodies and binaries.
- [x] 7.6 Add API tests for aggregate consistency, maximum windows, stable pagination tie breakers, invalid filters, CSV escaping/encoding, row caps, and export audit events.

## 8. Administrator console shell and navigation

- [x] 8.1 Restructure `AdminSection` and route parsing into grouped dashboard, users, finance, content/assets, AI operations, platform configuration, and audit/operations sections while preserving supported old deep links.
- [x] 8.2 Build a dedicated responsive administrator shell that removes creator workflow chrome and exposes only system-operation navigation.
- [x] 8.3 Add an independent administrator route/authentication gate that bypasses normal `AuthGate`/`WorkspaceGate` and denies normal users before mounting console pages or requesting administrator data.
- [x] 8.4 Build the dashboard view with bounded period selection, financial/task/user summaries, exception queues, loading/empty/error states, and drill-down links.
- [x] 8.5 Replace browser-native reason/credential prompts in existing user and ticket actions with accessible Chinese confirmation and one-time-result dialogs.
- [x] 8.6 Add URL-backed filters and stable pagination controls shared across administrator list pages.
- [x] 8.7 Add frontend route/navigation tests covering independent administrator login/session handling, normal-user denial, old-link compatibility, deep links, mobile layout, and no creator chrome.

## 9. User management and user detail UI

- [x] 9.1 Remove administrator-flag filtering from the normal-user directory and retain ID/phone/status/date/wallet-exception filters, deterministic pagination, masked identity, and operational summary columns.
- [x] 9.2 Preserve audited atomic user creation and build explicit dialogs for suspension/reactivation, session revocation, and reset-credential issuance.
- [x] 9.3 Move sole-administrator self-protection out of normal-user operations and add frontend/backend tests against the independent administrator account.
- [x] 9.4 Build the administrator user-detail route and overview with authoritative account, balance, activity, counts, recent exceptions, and direct operational actions.
- [x] 9.5 Build lazy paginated tabs for workspaces, projects/series/scripts, characters/scenes/props/media, AI tasks, orders/wallet/ledger/usage, and security/audit.
- [x] 9.6 Add user/workspace context filters to every detail tab and reject or clear stale workspace selections when the target user changes.
- [x] 9.7 Build purpose dialogs and audited viewers for script/prompt text, task diagnostics, and private media previews without exposing raw storage details.
- [x] 9.8 Add frontend tests for lazy loading, filter isolation, masking, sensitive-purpose requirements, preview expiry/errors, and absence of inspection mutations.

## 10. Finance and recharge order UI

- [x] 10.1 Build the manual recharge order directory with order/user/status/operator/reference/date filters, deterministic pagination, totals, and CSV export.
- [x] 10.2 Build a pending-order creation dialog with CNY fen-safe amount input, microticket/ticket conversion display, offline reference, target user confirmation, reason, and idempotency handling.
- [x] 10.3 Build order detail with immutable event timeline, wallet/ledger correlation, cash/ticket snapshots, actor/time metadata, and manual-confirmation labeling.
- [x] 10.4 Build explicit complete and cancel dialogs with current-version conflict recovery and one-submit loading behavior.
- [x] 10.5 Build partial/full refund dialogs that show remaining refundable cash/tickets, current available balance, held tickets, and server-returned shortfall/conflict details.
- [x] 10.6 Reorganize existing ticket adjustments and usage views under finance, clearly separating gifts/compensation/debits from paid recharge revenue.
- [x] 10.7 Build reconciliation status and mismatch views that never offer silent repair or mutable historical edits.
- [x] 10.8 Add frontend tests for amount precision, state-specific actions, duplicate submits, stale versions, insufficient balance, event rendering, and paid-versus-adjustment terminology.

## 11. System scene management UI

- [x] 11.1 Build the system scene directory with ID/name/category/tag/visibility/lifecycle/version/date filters, stable ordering, usage counts, and cover previews.
- [x] 11.2 Build create/edit forms for the validated scene schema with field-level Chinese errors, media upload state, and optimistic version conflict handling.
- [x] 11.3 Build enable/disable/reorder/archive actions with required reasons, reference-count warnings, confirmations, and audit-aware success/error feedback.
- [x] 11.4 Add media replacement and preview behavior that uses system-media APIs and never stores raw object keys in browser state.
- [x] 11.5 Integrate the read-only enabled system-scene catalog into the existing user scene-selection flow and preserve copy/snapshot provenance.
- [x] 11.6 Add frontend tests for normal-user read-only behavior, administrator mutations, stale edits, referenced-scene warnings, disabled/archive visibility, and copied-scene stability.

## 12. Security, observability, and localization

- [x] 12.1 Isolate administrator CSRF cookies, origin validation, session lifecycle, and rate-limit namespaces from normal-user authentication across all administrator mutations, sensitive previews, and exports.
- [x] 12.2 Store independent administrator actor IDs in new audit events and correlations for console entry failures, user operations, orders, refunds, ticket adjustments, configuration, system scenes, sensitive reads, exports, imports, and reconciliation runs while preserving historical user-actor fields.
- [x] 12.3 Add structured metrics for admin API latency/errors, order transitions/idempotency conflicts, reconciliation mismatches, sensitive previews, dashboard query time, and system-scene operations without logging private content.
- [x] 12.4 Add log redaction tests for phones, scripts/prompts, reset credentials, object keys, provider diagnostics, offline evidence, and idempotency secrets.
- [x] 12.5 Complete Chinese administrator copy and terminology checks for every new route, dialog, state, error, empty state, CSV header, order status, and scene lifecycle label.
- [x] 12.6 Add accessibility checks for keyboard navigation, focus management, dialog labeling, error association, table/list semantics, and responsive text containment.
- [x] 12.7 Add explicit guardrail tests proving there is no payment-provider claim/integration, administrator impersonation endpoint, multi-role RBAC behavior, user-asset mutation, or hard-delete path in this release.

## 13. End-to-end verification and operations

- [x] 13.1 Run the complete backend test suite and frontend unit/component/type/build suites after integrating independent administrator identity.
- [x] 13.2 Add PostgreSQL integration tests for independent administrator RLS versus normal-user RLS across users, workspaces, assets, media, tasks, orders, ledgers, and reused pooled connections.
- [ ] 13.3 Execute a disposable real-Chromium end-to-end workflow covering independent admin login, dashboard, user creation/detail, manual recharge completion/refund, user wallet history, system scene creation/copy, sensitive preview audit, logout/session rejection, and normal-user denial; do not commit browser automation code.
- [x] 13.4 Add concurrency/load tests for order completion/refunds, dashboard bounds, user-detail pagination, and large asset/task directories.
- [x] 13.5 Extend Docker Compose smoke verification to migrate from the current head, independently bootstrap `admin`, seed a system scene, execute a manual recharge transaction, and verify frontend/backend readiness.
- [x] 13.6 Update administrator navigation and operation runbooks for independent login, credential recovery, manual order semantics, gift-versus-paid distinctions, refund failure handling, scene lifecycle, sensitive-access policy, exports, and reconciliation.
- [x] 13.7 Update deployment documentation for independent administrator bootstrap, default-password restrictions, feature-flag enablement, migration/backfill checks, reconciliation acceptance criteria, rollback limits, and incident disable procedures.
- [ ] 13.8 Run OpenSpec validation and confirm every requirement scenario has an implementation task and automated or documented verification evidence.

## 14. Physical administrator identity separation

- [x] 14.1 Add `admin_users` and `admin_sessions` SQLAlchemy models and an Alembic migration using auto-increment integer primary keys, indexed logical relationships, checks/uniqueness, and zero foreign keys.
- [x] 14.2 Implement `/api/v1/admin/auth/login`, session lookup, CSRF proof, logout, password change/recovery, expiry, revocation, and dedicated `lumenx_admin_session`/`lumenx_admin_csrf` cookies without using normal-user authentication records.
- [x] 14.3 Add administrator-specific actor columns and indexes to audit, recharge order/event, ticket adjustment/ledger correlation, configuration, system scene, export/import, and sensitive-access records; preserve historical actor fields and make all new privileged writes use `admin_id` only.
- [x] 14.4 Revoke legacy shared-identity privileged sessions, remove `users.is_platform_admin` from models, APIs, frontend types, filters, runtime authorization, and the database schema, while preserving former administrator users and all of their user-owned data as ordinary users.
- [x] 14.5 Refactor every administrator API, service, repository, idempotency scope, audit call, filter, and projection to consume `AdminContext` and independent administrator IDs without equating `admin_users.id` and `users.id`.
- [x] 14.6 Implement an idempotent post-migration Compose bootstrap that creates only `admin_users(username=admin)` with local password `sk532359025`, creates no wallet/workspace/assets, preserves an existing password on rerun, and rejects the local default in non-local deployment.
- [x] 14.7 Build an independent Chinese administrator login/logout/session experience and API client behavior that can coexist with a normal-user login in the same browser without cookie or CSRF confusion.
- [x] 14.8 Add focused migration, authentication, bootstrap, cross-session rejection, actor-integrity, former-admin demotion, and no-user-domain-side-effect tests for the physical separation boundary.
- [x] 14.9 Add an explicit audited recovery path that adopts exactly one differently named independent administrator into the configured bootstrap identity while preserving its ID/history, rotating credentials, revoking administrator sessions, retaining normal bootstrap fail-closed behavior, and supporting PostgreSQL RLS.
- [x] 14.10 Enable a local-Compose-only automatic adoption opt-in so retained volumes with exactly one differently named administrator converge to `admin` during ordinary startup while preserving ID/history, revoking old sessions, remaining idempotent on rerun, and keeping production/release bootstrap fail closed.
