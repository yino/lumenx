## Why

The existing platform administration surface covers basic user security actions, ticket adjustments, task lists, configuration, and audit events, but it does not provide a complete system-operator workflow for manual recharge orders, platform scene templates, or an auditable per-user view of workspaces and creative assets. Its administrator identity also shares the normal `users` and session domain, which gives a system operator unnecessary user wallet/workspace semantics and weakens the intended physical boundary. A dedicated system administrator identity and console are needed so one super administrator can operate, reconcile, and support the cloud platform without becoming a front-office user or bypassing ownership controls.

## What Changes

- Restructure the existing administration surface into a dedicated `/admin` console with an operator dashboard and grouped navigation for users, finance, content/assets, AI operations, configuration, and audit/operations.
- **BREAKING** Physically separate system administrators from front-office users with auto-increment integer `admin_users` and `admin_sessions` records, independent administrator authentication endpoints, cookies, CSRF state, rate limits, and session lifecycle. Normal `users` records no longer grant administrator authority.
- Bootstrap the single administrator account independently as username `admin` with the configured initial password, without creating a user wallet, workspace, project, or user asset. Existing shared-identity administrator data is migrated or explicitly retired without creating a second user-domain administrator.
- Record privileged actors through administrator identifiers in audits, manual recharge orders/events, ticket ledger correlations, configuration changes, system scenes, exports, imports, and sensitive-access history rather than overloading user identifiers.
- Add a system-administrator user detail view that aggregates account security, workspaces, projects, series, scripts, characters, scenes, props, media, AI tasks, usage, wallet, orders, and related audit history.
- Add manual recharge order management for the first release. Orders represent administrator-recorded purchases of tickets, are distinct from gifts/compensation/debits, use an explicit lifecycle, and post immutable wallet and ledger entries exactly once.
- Add cancellation and refund/reversal handling for manual recharge orders with nonnegative-balance enforcement, mandatory reasons, idempotency, and reconciliation.
- Add a platform-owned system scene catalog that administrators can create, edit, enable, disable, order, and archive; normal users can consume enabled templates but cannot mutate them.
- Add cross-user, read-only administrator inspection of user-owned workspaces, content, assets, media metadata/previews, and AI tasks through explicit administrator APIs with masking and access auditing.
- Expand AI operations and finance views with user/workspace filters, task detail, settlement state, order-ledger correlation, exception queues, and dashboard summaries.
- Retain one system super-administrator account for the first release, without multi-role RBAC; every administration route and action remains inaccessible to normal-user sessions.
- Explicitly exclude user-facing administration, external payment integrations, multi-role RBAC, administrator impersonation, unrestricted asset downloads, and hard deletion of user-owned content from this change.

## Capabilities

### New Capabilities
- `system-admin-console`: Physically separate administrator identity/session boundary, dedicated administrator-only shell, grouped navigation, operations dashboard, and common query/export behavior.
- `admin-user-operations`: System administrator user management and a consolidated per-user operational detail view.
- `manual-recharge-orders`: Manual ticket recharge order creation, lifecycle, posting, cancellation, refund/reversal, and reconciliation.
- `system-scene-catalog`: Platform-owned reusable scene-template administration and read-only user consumption.
- `admin-resource-oversight`: Audited, read-only cross-user inspection of workspaces, projects, series, scripts, assets, media, tasks, usage, and related operational exceptions.

### Modified Capabilities
- `ticket-accounting`: Distinguish paid manual recharge credits from grants and compensation, correlate order posting/refunds with immutable ledger entries, and preserve wallet invariants.
- `workspace-data-isolation`: Add an explicit platform-administrator inspection path established by an independent administrator session that remains separate from normal user scope, is read-only by default, and is fully audited.

## Impact

- Frontend: independent administrator authentication gate and session handling, administrator routing, navigation, dashboard, user detail, order management, system scene catalog, resource inspection, task/finance detail, Chinese copy, and admin-specific tests.
- Backend: independent administrator authentication/session services and `/admin/auth/*` endpoints; new `/admin/*` query and mutation endpoints, recharge order and system scene services, administrator inspection projections, filtering/pagination/export boundaries, audit events, and reconciliation jobs.
- Database: integer-ID `admin_users` and `admin_sessions`; administrator actor columns and migration of shared-identity authority; integer-ID tables for recharge orders and order events; validated system-scene fields on existing system-scoped assets; a private system-media namespace; supporting indexes, immutable-state constraints, and row-level-security policies. No foreign-key constraints are introduced.
- Existing ticket wallet, ledger, task, workspace, project, series, asset, media, configuration, and audit records are reused rather than duplicated.
- Operations: independent administrator credential bootstrap and recovery, shared-identity retirement, manual order reference conventions, refund/reversal runbooks, sensitive-data access policy, dashboard metrics, and migration/rollback verification.
