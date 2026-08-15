## Why

LumenX currently assumes one trusted local user, shared JSON files, shared output directories, process-wide model credentials, and user-selectable models. To operate as a hosted product, it needs authenticated users, workspace-level isolation, auditable usage billing, server-controlled AI execution, centrally managed model configuration, and a Chinese-only product surface without discarding the existing desktop workflow.

## What Changes

- Add phone-number-and-password registration, login, logout, password reset administration, account status, and secure server-side sessions. SMS verification is not enforced in the first release, but the account schema and service boundary reserve phone-verification state and delivery hooks.
- Treat `User` as the ownership, security, and billing boundary; do not introduce tenant or organization entities. Allow each user to create multiple isolated workspaces.
- Move authoritative projects, series, scripts, asset metadata, task state, and usage records from shared JSON files into PostgreSQL records scoped by `user_id` and `workspace_id`.
- Isolate generated media in OSS by user/workspace/project prefixes and authorize every media read or signed-URL operation. Provide an explicit importer for existing local JSON and `output/` data.
- Add an immutable ticket wallet and ledger. AI usage is normalized into internal metering tokens, converted using a versioned `n tokens = 1 ticket` rule, reserved before execution, and settled or released after execution.
- Route every AI operation through a server-side AI gateway and persistent worker queue. The browser may submit content and allowed business parameters but cannot choose providers, models, prices, or credentials.
- **BREAKING** Remove user-facing model selection and stop treating project or series `model_settings` as execution authority.
- Add database-backed model configuration by capability, including active and fallback models, allowed parameters, metering formulas, availability, and versioned configuration snapshots. Platform credentials remain in server-side secret storage, not the database config rows.
- **BREAKING** Make the user-visible application Chinese-only. Remove the language selector and English chrome while retaining technical identifiers, provider brands, internal prompts, logs, and the existing i18n structure where useful.
- Add a protected administration surface for user status, ticket adjustments, usage investigation, AI task inspection, model configuration, exchange-rate management, and audit history.
- Preserve desktop mode through storage, identity, AI execution, and task-runner adapters; desktop mode remains usable without cloud authentication.

## Capabilities

### New Capabilities
- `phone-user-auth`: Phone-and-password account registration and secure session management with reserved phone-verification support.
- `workspace-data-isolation`: User-owned workspaces with isolated database records, asset libraries, tasks, and media access.
- `ticket-accounting`: Metering-token normalization, ticket conversion, balance reservation, settlement, refunds, immutable ledger entries, and usage reporting.
- `server-ai-gateway`: Server-authoritative AI requests, persistent task execution, provider credential protection, idempotency, and result handling.
- `database-model-configuration`: Administrator-managed model routing, parameters, pricing formulas, availability, fallbacks, and immutable execution snapshots.
- `chinese-only-ui`: Chinese-only user-visible navigation, controls, status, validation, and error presentation while preserving necessary technical names and internal prompts.
- `desktop-cloud-compatibility`: Explicit desktop and cloud adapters plus controlled import of existing local projects and media into a selected user workspace.

### Modified Capabilities

None. No existing OpenSpec capabilities are present in this repository.

## Impact

- Backend: FastAPI authentication and authorization dependencies, domain services, repositories, AI gateway, worker execution, billing, administration APIs, and removal or protection of desktop-only diagnostic/config endpoints in cloud mode.
- Data: PostgreSQL schema and migrations for users, sessions, workspaces, content documents, assets, media, model configuration, tasks, wallets, holds, ledger entries, usage events, and audit logs; Redis-backed persistent job dispatch; OSS namespace and access changes.
- Frontend: registration/login flows, authenticated API client, workspace management and switching, ticket balance/ledger views, administration screens, model-selector removal, cache scoping, and Chinese-only visible copy.
- Operations: platform-managed provider secrets, database/Redis/OSS deployment, backup and retention policies, observability, rate limits, and migration tooling.
- Compatibility: existing local Pydantic workflow objects and AI generation logic remain reusable behind adapters; existing per-project model settings become historical data only.
