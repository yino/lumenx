## Context

LumenX is currently a trusted single-user desktop/local application. FastAPI constructs one process-wide `ComicGenPipeline`, loads projects, series, and the global asset library from JSON files, exposes the shared `output/` tree through static routes, executes long-running work with in-process background tasks, and reads provider credentials from process environment variables. The Next.js frontend has no authenticated principal, persists some project/configuration state in browser storage, exposes provider/model settings, and intentionally supports both Chinese and English chrome.

The hosted product changes the trust boundary. A user owns multiple workspaces, and every content record, AI task, media object, and charge must be attributable to that user and one workspace. The browser is untrusted: it must not choose the billable model, supply a price, access an arbitrary media path, or mutate platform credentials. The desktop build must continue to work without cloud authentication and without bundled platform credentials.

The first release uses phone number plus password. SMS verification is deliberately deferred. A phone number is therefore a unique login identifier, not proof that the user controls that number. The schema reserves verification state and the service boundary reserves a verification provider, but self-service SMS recovery is not enabled until ownership can be verified.

## Goals / Non-Goals

**Goals:**

- Authenticate cloud users with secure server-side sessions and make `User` the ownership and billing boundary.
- Allow each user to own multiple isolated workspaces without introducing tenants, organizations, teams, or invitations.
- Move cloud authority from shared JSON/filesystem state to PostgreSQL and private OSS objects.
- Meter every server-side AI operation, convert normalized metering tokens to user-visible tickets, and maintain a financially auditable immutable ledger.
- Make model routing, allowed parameters, pricing, and fallback behavior administrator-controlled and database-backed.
- Route all cloud AI work through an idempotent server gateway and persistent worker queue.
- Present a Chinese-only user interface while retaining provider brands, technical identifiers, internal prompts, and translation infrastructure where useful.
- Preserve the local desktop experience through explicit adapters and provide controlled local-to-cloud import.

**Non-Goals:**

- SMS delivery, phone ownership verification, SMS-based password recovery, OAuth, social login, or MFA in the first release.
- Tenants, organizations, shared workspaces, member invitations, or workspace roles beyond owner access.
- Online payment, invoicing, subscriptions, coupon campaigns, or automatic purchasing; administrators adjust balances in the first release.
- User-supplied provider credentials in cloud mode.
- Real-time collaborative editing or cross-user project/asset sharing.
- Full relational normalization of every nested `Script`, `Series`, frame, and asset field in the first migration.
- Translating source code, API field names, database identifiers, logs, internal model prompts, model IDs, or provider brand names.

## Decisions

### 1. User is the ownership and billing boundary

Cloud resources use `user_id` directly. There is no `tenant` table and no membership join table in this change. A user owns zero or more workspaces, and a workspace belongs to exactly one user.

Every workspace-scoped table carries both `user_id` and `workspace_id`. Although `workspace_id` can imply the owner, duplicating `user_id` makes authorization queries explicit, supports PostgreSQL row-level security, and prevents accidental unscoped joins. Composite foreign keys or equivalent repository validation ensure the workspace belongs to the same user.

Alternative considered: retain a hidden one-to-one tenant for future teams. Rejected because the user explicitly chose a user-only ownership model. Adding organizations later will require a deliberate ownership migration instead of carrying unused tenant complexity now.

### 2. Phone/password identity with opaque server sessions

`users` stores a canonical E.164-like phone value, password hash, status, `phone_verified_at`, timestamps, and platform-admin flag. The UI defaults to country code `+86`; normalization happens server-side and canonical phone values are unique. Passwords use Argon2id through a maintained password-hashing library.

Registration creates the user, wallet, configured initial-grant ledger entry, and a default workspace in one transaction. The configurable initial grant defaults to zero until an administrator explicitly changes it.

The server issues a high-entropy opaque session token in a `Secure`, `HttpOnly`, `SameSite=Lax` cookie. Only a hash of that token is stored in `auth_sessions`; sessions have absolute and idle expiry and can be revoked individually or for the whole user. State-changing requests use same-origin enforcement plus a CSRF token/header. Login and registration are rate-limited by phone and network source.

Because phone ownership is not verified, `phone_verified_at` remains null and no SMS recovery is offered. Authenticated users can change their password after re-entering the current password. A platform administrator can revoke sessions and issue a short-lived, single-use reset credential through an out-of-band support process. A future `PhoneVerificationProvider` interface owns send/verify behavior without changing account ownership.

Alternative considered: browser-stored JWT. Rejected because long-lived browser tokens are harder to revoke and increase exposure to script injection. Opaque server sessions fit the same-origin static frontend and FastAPI backend.

### 3. PostgreSQL repositories with document-shaped content

PostgreSQL is authoritative in cloud mode. Initial tables include:

- `users`, `auth_sessions`, `workspaces`
- `projects`, `series`, `assets`, `media_objects`
- `ai_tasks`, `usage_events`
- `ticket_wallets`, `ticket_holds`, `ticket_ledger`
- `model_configs`, `platform_configs`, `config_versions`
- `audit_events`, `import_batches`

Frequently queried identity and relationship fields are relational. Existing nested project and series state is stored in validated JSONB payloads with a schema version, plus relational summary columns such as title, series ID, timestamps, and optimistic `version`. This preserves existing Pydantic domain logic while removing whole-file writes. Later normalization can be incremental.

Repository methods require an explicit `UserContext` and `workspace_id`; there are no unscoped `get(id)` or `list_all()` methods in cloud services. PostgreSQL RLS uses the transaction-local authenticated user ID as defense in depth. Unauthorized and nonexistent resource IDs both return 404 to avoid existence disclosure.

Optimistic writes compare the record `version`. A stale update returns a conflict instead of silently overwriting another browser session.

### 4. Workspace-scoped asset and media ownership

The asset resolver supports four explicit scopes:

1. `project`: private to one project.
2. `series`: shared by projects in one series.
3. `workspace`: reusable throughout one user-owned workspace; this replaces the current process-global library.
4. `system`: platform-curated, read-only assets available to all users.

No user-owned asset can cross a workspace boundary. Promotion/copy creates or updates an asset in a permitted destination scope and records provenance; it never changes ownership by editing a path string.

Binary objects live in private OSS keys shaped as `users/{user_id}/workspaces/{workspace_id}/projects/{project_id-or-shared}/...`. `media_objects` stores ownership, object key, MIME type, size, checksum, lifecycle state, and provenance. Browser APIs exchange media IDs, not filesystem paths or arbitrary object keys. The server authorizes access before issuing short-lived signed URLs. Cloud mode does not mount the whole output directory as public static files.

### 5. Immutable ticket accounting with normalized metering tokens

`ticket_wallets` stores cached integer balances in microtickets, where one displayed ticket equals 1,000,000 microtickets. `ticket_ledger` is append-only and is the audit source of truth. Adjustments are represented as new compensating entries; historical entries are never edited or deleted.

Every model configuration defines a deterministic metering formula:

- LLM: weighted actual input and output provider tokens.
- Image: operation, image count, resolution, and relevant premium options.
- Video: operation, duration, resolution, audio option, and output count.
- Speech/audio: characters, provider tokens, or duration according to provider semantics.
- Unsupported or unmeterable operations remain disabled until a bounded formula exists.

The active versioned `platform_configs.tokens_per_ticket` defines `1 ticket = n metering tokens`. Conversion uses integer arithmetic:

`microtickets = ceil(metering_tokens * 1_000_000 / tokens_per_ticket)`.

The task stores immutable snapshots of model config, metering formula, and exchange rate. Later configuration changes affect only newly created tasks.

Task creation runs one database transaction that validates ownership and parameters, calculates a maximum bounded quote, locks the wallet row, checks available balance, creates a hold, appends the reservation ledger event, and creates the AI task. A unique `(user_id, idempotency_key)` constraint returns the original task for client retries.

Workers never charge balances directly. On terminal provider outcome they call a billing settlement service in a database transaction:

- Success: calculate actual metering tokens, debit actual microtickets, release unused hold, append settlement and release entries, and store a usage event.
- Provider-billed success followed by local post-processing failure: settle the provider-backed amount and mark the task for support review.
- Failure before provider billing or accepted cancellation: release the entire hold.
- Retry: reuse the same task/hold where provider semantics prove no new billable request; otherwise create an explicitly linked attempt and reserve separately.

The LLM quote reserves against the configured maximum response-token bound so actual use cannot exceed the hold. Media formulas are deterministic from accepted parameters. Negative available balance is prohibited.

Initial grants and manual administrator adjustments use typed ledger entries. Online payment is intentionally deferred.

### 6. Server-side AI gateway and persistent workers

Cloud clients submit a capability, workspace/project references, prompt/content, media IDs, allowed business parameters, and idempotency key. They cannot submit a provider, provider model ID, price, metering formula, credential, arbitrary endpoint, or local path.

`AIGateway` performs:

1. Authentication and resource ownership validation.
2. Server-side model configuration selection and allowed-parameter normalization.
3. Quote and wallet reservation.
4. Persistent task creation and enqueue.
5. Status/result projection without provider secrets.

Celery workers consume Redis-dispatched task IDs, reload the task and immutable configuration snapshot from PostgreSQL, resolve authorized media, obtain credentials through a server `CredentialProvider`, call the provider adapter, persist provider request IDs, poll or accept callbacks, upload results, and settle billing. PostgreSQL remains authoritative; Redis is never the only copy of task state.

The task state machine is `reserved -> queued -> running -> provider_succeeded -> succeeded`, with terminal `failed` and `cancelled` paths. State transitions are compare-and-set and auditable. Provider submission identifiers prevent blind resubmission after worker restart.

Existing provider adapters and generation logic are retained behind request-scoped configuration. Process-wide environment mutation, process-wide provider clients that change with user input, and FastAPI `BackgroundTasks` are prohibited in cloud execution.

### 7. Typed database model and platform configuration

`model_configs` contains one versioned model route per capability/provider model combination: capability, Chinese display name, provider, provider model ID, enabled state, priority, default and allowed parameter schema, metering formula, secret reference, and effective timestamps. One enabled primary route exists per required capability; ordered enabled routes provide fallback.

`platform_configs` holds schema-validated, versioned global settings such as tokens-per-ticket, registration initial grant, session limits, and concurrency limits. It is not a general unvalidated key/value dumping ground. Secret values are forbidden in both tables; `secret_ref` resolves to deployment environment or a secret manager.

Admin writes create a new configuration version, validate capability completeness and formulas, append an audit event, and atomically activate it. Workers execute the snapshot attached to their task, not the latest mutable row. The existing YAML catalog seeds the first database records but ceases to be cloud runtime authority.

Existing `Script.model_settings` and `Series.model_settings` remain readable for migration/history but are ignored by cloud execution. All frontend model selectors and API fields that attempt to override execution model are removed or rejected in cloud mode. Task detail may show the Chinese display name and immutable technical model ID read-only.

### 8. Chinese-only product surface

The runtime locale is fixed to Chinese. The language selector and English display mode are removed. Existing `next-intl` message lookup may remain so Chinese copy stays centralized and testable. All user-visible navigation, labels, helper text, statuses, validation messages, empty states, toasts, dialogs, administrative pages, and safe backend error projections are Chinese.

Provider brands (`Wan`, `Kling`, `Vidu`), technical abbreviations (`R2V`, `TTS`, `API`), model IDs, user-authored content, and media metadata remain unchanged where translation would reduce correctness. Deliberate English eyebrows and secondary English titles are removed or replaced with Chinese. Internal model prompts remain in the language required for output quality and are never interpreted as UI copy.

The product term displayed for `ticket` is `算力券`.

### 9. Cloud and desktop modes use adapters, not scattered branches

Configuration selects `desktop` or `cloud` at composition time:

- Desktop: local principal, JSON repository, local filesystem storage, local user-supplied provider credentials, and in-process runner. Ticket billing is disabled because platform credentials are not used.
- Cloud: authenticated user context, PostgreSQL repository, private OSS storage, platform credential provider, Celery runner, and mandatory ticket billing.

Domain services depend on repository, storage, credential, model-config, task-runner, and billing interfaces. They do not inspect deployment mode inside individual business operations.

Cloud mode disables or platform-admin-protects process diagnostics, log-tail access, environment mutation, local CLI login, and broad static-file mounts. Platform credentials are never bundled into desktop artifacts.

### 10. Administration and audit are first-class

A platform administrator can suspend/reactivate users, revoke sessions, issue reset credentials, add or subtract tickets with a reason, inspect ledger/usage/task correlation, manage model/platform configurations, and inspect import batches. Every administrative mutation records actor, target, reason, before/after summary, request correlation ID, and timestamp.

User-facing usage history exposes balance, held amount, operation, project/workspace, metering tokens, ticket amount, status, and time without exposing provider secrets or internal costs. Provider raw cost and margin fields, if recorded, are administrator-only.

## Risks / Trade-offs

- [Unverified phone numbers can be mistyped or claimed by someone else] -> Label accounts unverified, do not use the number as proof of identity, rate-limit registration, provide administrator recovery, and add verification hooks before enabling SMS recovery.
- [User-only ownership makes future team accounts a migration] -> Keep ownership access behind repositories and avoid embedding user IDs inside opaque document payloads where possible.
- [JSONB documents can still produce write conflicts and large rows] -> Use optimistic versions, relational summary/index columns, bounded payload validation, and normalize high-churn entities such as tasks and media immediately.
- [A billing bug can create financial loss or user distrust] -> Use integer units, immutable ledgers, holds, database row locks, idempotency constraints, configuration snapshots, invariant tests, and reconciliation jobs.
- [Provider usage may be missing or inconsistent] -> Disable models without a bounded deterministic fallback formula; store raw provider usage and formula inputs for replayable reconciliation.
- [Worker crash after provider submission can duplicate expensive work] -> Persist provider request IDs before polling, use compare-and-set task transitions, and make retries provider-aware rather than blindly resubmitting.
- [RLS context can leak across pooled connections] -> Set user context transaction-locally, reset it automatically, require repository scopes, and test connection-pool reuse across users.
- [Signed URLs can outlive authorization changes] -> Use short expirations, private buckets, non-guessable keys, and avoid embedding credentials in persisted project payloads.
- [Removing user model selection reduces expert control] -> Preserve allowed creative parameters and expose the actual model read-only in task details; administrators control routing and fallbacks centrally.
- [Maintaining desktop and cloud adapters increases test combinations] -> Define contract tests for every adapter and keep cloud-only concerns outside domain models.
- [A literal English-string purge can damage model prompts and technical correctness] -> Scope Chinese-only enforcement to rendered user-facing copy and maintain an approved technical-name allowlist.

## Migration Plan

1. Add cloud dependencies and environment composition without changing desktop defaults.
2. Create PostgreSQL, Redis, and private OSS infrastructure; apply database migrations and seed platform/model configuration from the current catalog.
3. Implement identity/session tables and APIs, create wallet/default-workspace records transactionally, and add authenticated frontend routing.
4. Introduce repository/storage interfaces and cloud implementations; migrate project, series, asset, media, and task APIs to require user/workspace context.
5. Add AI gateway, persistent task state machine, worker execution, provider idempotency metadata, and billing holds/ledger settlement behind a disabled cloud feature flag.
6. Add administration and user usage surfaces, remove cloud model/config controls, and complete Chinese-only visible copy.
7. Run security, isolation, billing invariant, worker recovery, media authorization, and localization scans in staging.
8. Enable cloud registration for administrators, reconcile wallets/tasks, then progressively open user registration.
9. Ship the idempotent local importer with dry-run, checksum verification, import-batch audit, and explicit target user/workspace selection.

Rollback does not revert or delete ledger entries. Disable new AI task creation, drain or cancel queued tasks according to provider state, settle/release all holds, switch traffic back to the previous API release, and preserve the new database for reconciliation. Database migrations use expand/contract sequencing so the previous release can coexist during rollout. Imported content is tagged by `import_batch_id`; rollback marks the batch reverted and deletes only unreferenced records/media created by that batch.

## Open Questions

No blocking product questions remain. The initial registration ticket grant defaults to zero and online payment remains out of scope until an operator configures a grant or adjusts balances through the administration surface.
