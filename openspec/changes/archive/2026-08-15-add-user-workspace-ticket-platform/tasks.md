## 1. Cloud foundation and composition

- [x] 1.1 Add pinned backend dependencies for SQLAlchemy 2, Alembic, PostgreSQL, Argon2 password hashing, Redis, Celery, and structured settings to development and Docker requirement files.
- [x] 1.2 Define validated `desktop` and `cloud` deployment settings, with cloud startup rejecting missing database, Redis, private OSS, session-secret, model-config, or provider-secret prerequisites.
- [x] 1.3 Introduce deployment-neutral interfaces for identity context, repositories, media storage, credential resolution, model configuration, task dispatch, and billing.
- [x] 1.4 Add PostgreSQL engine/session management with transaction-local authenticated user context and safe connection-pool reset behavior.
- [x] 1.5 Initialize Alembic and create identity/workspace migrations for `users`, `auth_sessions`, password reset credentials, and `workspaces`.
- [x] 1.6 Create content/storage migrations for `projects`, `series`, `assets`, and `media_objects` with user/workspace scope, JSONB schema versions, optimistic versions, soft-delete fields, and composite ownership constraints.
- [x] 1.7 Create execution/billing migrations for `ai_tasks`, task attempts, `usage_events`, `ticket_wallets`, `ticket_holds`, and append-only `ticket_ledger` tables.
- [x] 1.8 Create administration/configuration migrations for `model_configs`, `platform_configs`, `config_versions`, `audit_events`, and `import_batches`.
- [x] 1.9 Add indexes, uniqueness constraints, task/idempotency constraints, append-only ledger protections, and PostgreSQL row-level security policies for every user-owned table.
- [x] 1.10 Directly extend the repository's existing `docker-compose.yml` with PostgreSQL, Redis, AI worker, migration, health/readiness, named database persistence volume, private OSS, backup, and secret-injection configuration while preserving desktop defaults; do not create a separate Compose file.

## 2. Phone authentication and sessions

- [x] 2.1 Implement `+86`-default phone normalization, canonical uniqueness validation, password policy, and Argon2id hash/verify services.
- [x] 2.2 Implement atomic registration that creates the unverified user, configured initial-grant ledger entry, wallet, default workspace, and first session or rolls back all records.
- [x] 2.3 Implement opaque session creation, token hashing, Secure/HttpOnly/SameSite cookie delivery, idle/absolute expiry, refresh, current-user resolution, logout, and all-session revocation.
- [x] 2.4 Add same-origin and CSRF protection for cookie-authenticated mutations and configurable registration/login/reset rate limits.
- [x] 2.5 Add Chinese registration, login, logout, current-profile, authenticated password-change, and session-management APIs with nondisclosing credential errors.
- [x] 2.6 Reserve phone-verification state and a `PhoneVerificationProvider` interface while keeping SMS send/verify and SMS recovery disabled.
- [x] 2.7 Implement audited platform-admin user suspension/reactivation, session revocation, and short-lived single-use support reset credentials.
- [x] 2.8 Add backend tests for duplicate/invalid phone registration, transaction rollback, password hashing, login nondisclosure, expiry/revocation, CSRF, rate limiting, suspended accounts, and unverified recovery behavior.

## 3. Workspace-scoped data persistence

- [x] 3.1 Implement user-owned workspace create, list, rename, select, soft-delete, restore, and 30-day cleanup services and APIs.
- [x] 3.2 Implement PostgreSQL project and series repositories that validate Pydantic JSONB payloads, require user/workspace context, and enforce optimistic version conflicts.
- [x] 3.3 Migrate project/series list, create, read, update, delete, episode-binding, prompt, and art-direction routes from the global Pipeline dictionaries to scoped services.
- [x] 3.4 Implement scoped project, series, workspace, and read-only system asset repositories with provenance and deterministic resolver priority.
- [x] 3.5 Migrate character, scene, prop, library, promotion, fork, import, voice, and variant APIs to authorized scope-aware asset services.
- [x] 3.6 Implement private OSS media storage under user/workspace/project prefixes and persist MIME type, size, checksum, lifecycle, and provenance in `media_objects`.
- [x] 3.7 Replace cloud path/object-key inputs and broad `/files` mounts with media-ID APIs, ownership checks, and short-lived authorized signed URLs.
- [x] 3.8 Migrate upload, storyboard, video, audio, export, and Playground media references from path authority to scoped media objects while preserving desktop path adapters.
- [x] 3.9 Scope Playground history, templates, outputs, saved assets, and task polling by authenticated user and workspace.
- [x] 3.10 Add cross-user, cross-workspace, RLS pool-reuse, asset-scope, media-signing, optimistic-conflict, soft-delete, and cleanup integration tests.

## 4. Database model and platform configuration

- [x] 4.1 Define typed schemas for model routes, allowed/default parameters, fallback eligibility, metering formulas, and platform settings including tokens-per-ticket, initial grant, sessions, and concurrency.
- [x] 4.2 Implement immutable config-version creation, complete-version validation, atomic activation, active-version caching/invalidation, rollback-by-new-version, and audit events.
- [x] 4.3 Build an idempotent seeder that maps the existing generated/YAML model catalog to inactive database model routes without overwriting administrator changes.
- [x] 4.4 Implement capability-based primary/fallback selection and request-scoped model clients that use the task snapshot rather than mutable process environment settings.
- [x] 4.5 Implement a server credential provider that resolves validated secret references and rejects plaintext secrets in model/platform configuration and audit payloads.
- [x] 4.6 Change cloud generation APIs to reject model/provider/endpoint/price overrides and ignore historical project/series `model_settings` during execution.
- [x] 4.7 Remove cloud user model-setting endpoints and model selectors from project, series, storyboard, video, Playground, and settings workflows; retain actual model details as read-only task audit data.
- [x] 4.8 Add platform-admin Chinese APIs and UI for drafting, validating, activating, disabling, and inspecting model/platform configuration versions with mandatory reasons.
- [x] 4.9 Add configuration tests for missing/duplicate primary routes, invalid parameters/formulas, secret rejection, atomic activation, fallback eligibility, immutable snapshots, seeder idempotency, and unauthorized access.

## 5. Ticket wallet and metering

- [x] 5.1 Implement integer microticket arithmetic and the versioned `ceil(metering_tokens * 1,000,000 / tokens_per_ticket)` conversion utility with overflow and boundary tests.
- [x] 5.2 Implement validated metering formula evaluators for LLM input/output tokens, images by count/resolution/options, video by count/duration/resolution/audio, and speech/audio usage.
- [x] 5.3 Implement wallet creation and cached available/held balance updates backed by append-only grant, hold, settlement, release, adjustment, and compensation ledger entries.
- [x] 5.4 Implement atomic quote/reservation with wallet row locking, maximum bounded usage, insufficient-balance rejection, task creation, and `(user_id, idempotency_key)` request fingerprinting.
- [x] 5.5 Implement successful settlement that records raw provider usage, normalized tokens, snapshot exchange rate, microticket charge, unused-hold release, and task/ledger correlation.
- [x] 5.6 Implement full release for nonbillable failure/cancellation and provider-billed partial-failure settlement with explicit support-review state.
- [x] 5.7 Implement retry accounting that reuses a hold only for proven nonbillable resumptions and creates a linked separately reserved attempt for potentially billable resubmission.
- [x] 5.8 Add platform-admin ticket grant/debit/compensation APIs and Chinese UI with required reasons, nonnegative-balance enforcement, and audit history.
- [x] 5.9 Add user Chinese wallet summary and paginated usage/ledger views showing available, held, operation, workspace/project, metering tokens,算力券 amount, status, and time.
- [x] 5.10 Implement a reconciliation command/job for wallet caches, ledger sums, stale holds, terminal tasks, and missing usage correlations without rewriting history.
- [x] 5.11 Add property/concurrency tests for rounding, exchange-rate snapshots, simultaneous reservations, idempotent retries, settlement/release races, negative-balance prevention, immutable corrections, and reconciliation.

## 6. Persistent AI gateway and workers

- [x] 6.1 Implement the persistent AI task and attempt state machine with atomic compare-and-set transitions, ownership, configuration snapshot, billing correlation, provider identifiers, cancellation, and support-review fields.
- [x] 6.2 Implement the authenticated AI gateway request contract using capability, content, owned resource/media IDs, allowed creative parameters, and required idempotency key.
- [x] 6.3 Integrate gateway validation, active model selection, parameter normalization, quote/hold creation, persistent task creation, and enqueue as one failure-safe workflow.
- [x] 6.4 Configure Celery/Redis dispatch so queue messages contain only task IDs and PostgreSQL remains authoritative for recovery and status.
- [x] 6.5 Implement worker task acquisition, duplicate-delivery exclusion, request-scoped credential/model construction, and safe provider invocation.
- [x] 6.6 Update provider adapters to persist provider request/task IDs and billable acknowledgement before polling or post-processing and to return normalized raw usage metadata.
- [x] 6.7 Implement restart recovery that resumes polling known provider tasks, reconciles ambiguous submissions, and never blindly resubmits expensive work.
- [x] 6.8 Implement authorized provider input resolution, output validation/download, private OSS upload, media-object creation, billing settlement, and safe Chinese result/error projection.
- [x] 6.9 Implement scoped task list/detail/status APIs and cancellation behavior that releases or settles holds only from confirmed provider state.
- [x] 6.10 Route script parsing, prompt polishing, asset generation, storyboard, image/video, Playground, TTS/voice, audio, and export-related AI calls through the gateway in cloud mode.
- [x] 6.11 Disable or platform-admin-protect cloud environment mutation, MuleRun local login, debug config, raw log-tail, system-path disclosure, direct provider routes, and legacy unmetered background-task entry points.
- [x] 6.12 Add worker/gateway integration tests for foreign media, client model overrides, insufficient balance, duplicate delivery, Redis loss, crash after provider acceptance, cancellation races, sensitive-error redaction, output failure after billing, and status isolation.

## 7. Authenticated workspace frontend

- [x] 7.1 Add Chinese phone registration and login screens with `+86` default, password validation, unverified-phone disclosure, safe errors, and authenticated-route guards.
- [x] 7.2 Refactor the frontend API client to use same-origin credentials, CSRF headers, stable Chinese error mapping, session-expiry handling, and no provider credentials or cloud model override fields.
- [x] 7.3 Add current-user/session state, secure logout, password change, suspended-account handling, and removal of sensitive persisted auth data.
- [x] 7.4 Add workspace creation, list, rename, switch, delete/restore, and current-workspace navigation with clear content context.
- [x] 7.5 Scope or clear Zustand/localStorage project, series, Playground, prompt, layout, and task caches by user/workspace on login, logout, and workspace switch.
- [x] 7.6 Update every project, series, asset, upload, task, Playground, generation, and media client call to include the selected workspace context and use media IDs/signed URLs.
- [x] 7.7 Add Chinese AI quote, insufficient算力券, held balance, queued/running/cancellation, billed partial-failure, and read-only actual-model states to generation workflows.
- [x] 7.8 Add Chinese platform-admin navigation and authorization guards for users, tasks, usage, ticket adjustments, configuration, audit events, and import batches.
- [x] 7.9 Add frontend tests for registration/login, route protection, workspace switching, cache isolation between users, media authorization failures, wallet history, model-selector absence, and admin access denial.

## 8. Chinese-only user experience

- [x] 8.1 Fix runtime locale to Chinese, migrate legacy English browser preference, remove the language selector, and prevent English message fallback.
- [x] 8.2 Replace deliberate English eyebrows, secondary titles, navigation, commands, statuses, helper copy, dialogs, toasts, empty states, and validation across all existing user workflows with Chinese.
- [x] 8.3 Complete Chinese copy for authentication, workspaces, wallet/usage, AI task states, billing disputes, model/config administration, user administration, and import administration using `算力券` consistently.
- [x] 8.4 Map expected backend auth, authorization, validation, billing, configuration, and AI failures to stable error codes with safe Chinese messages and correlation IDs.
- [x] 8.5 Define and review an allowlist for provider brands, model IDs, R2V/TTS/API terms, file formats, resolutions, IDs, and user-authored content that may remain canonical.
- [x] 8.6 Add rendered-page localization checks that fail on unapproved product-authored English without scanning source identifiers, internal prompts, logs, or provider payloads.
- [x] 8.7 Run Chinese copy and layout QA across desktop/mobile viewports to verify translated text does not overflow, overlap, or hide primary actions.

## 9. Desktop adapters and local-to-cloud import

- [x] 9.1 Wrap current JSON project/series/library and Playground persistence in desktop repository adapters satisfying the shared repository contracts.
- [x] 9.2 Wrap local output paths/static delivery, local credentials, in-process task execution, and no-op ticket billing in explicit desktop adapters.
- [x] 9.3 Refactor application composition so desktop and cloud select complete adapter sets at startup without per-operation deployment branches.
- [x] 9.4 Add build verification proving desktop artifacts contain no platform provider secret, secret-manager access, or unmetered cloud-provider fallback.
- [x] 9.5 Implement local import dry-run discovery and validation for projects, series, global/workspace assets, selected Playground history, path references, checksums, identifiers, capacity, and source fingerprints.
- [x] 9.6 Implement explicit target-user/workspace validation and idempotent import-batch persistence for document records, relationships, provenance, and private media uploads.
- [x] 9.7 Implement resumable import execution, final count/checksum/reference reconciliation, Chinese reports, and auditable batch status.
- [x] 9.8 Implement safe import rollback that soft-deletes batch-exclusive records and schedules only unreferenced imported media for cleanup.
- [x] 9.9 Add shared adapter contract tests and importer tests for target mismatch, broken references, duplicate retry, partial resume, integrity reconciliation, and rollback preservation.

## 10. Security, operations, and release verification

- [x] 10.1 Add audit middleware/correlation IDs and append audit events for authentication security actions, user status, ticket changes, configuration activation, task intervention, media administration, and imports.
- [x] 10.2 Add structured logs and metrics for registration/login abuse, workspace authorization denials, wallet invariants, task state latency, provider usage, settlement failures, queue depth, and media errors without secrets or raw user content.
- [x] 10.3 Add scheduled jobs for session expiry, soft-delete retention, orphan media, stale task/hold reconciliation, and audit/config backup verification.
- [x] 10.4 Add end-to-end adversarial tests covering cross-user/workspace IDs, RLS pool reuse, signed URL issuance, CSRF, credential leakage, arbitrary paths/endpoints, duplicate charges, and provider retry ambiguity.
- [x] 10.5 Add load tests for the initial target of 10,000 registered users, 10,000 daily AI tasks, user concurrency 2, and global worker backpressure with configured per-user limits.
- [x] 10.6 Document deployment, migrations, model/config seeding, first platform-admin creation, manual算力券 operations, backup/restore, reconciliation, secret rotation, and incident procedures in Chinese.
- [x] 10.7 Execute expand/contract staging migration, dry-run local import, wallet/task reconciliation, full backend/frontend/worker tests, and desktop regression tests before enabling cloud registration.
- [x] 10.8 Implement a staged cloud feature flag and rollback runbook that blocks new AI work, drains/reconciles tasks and holds, preserves immutable ledgers, and supports API rollback without destructive schema reversal.
