## Verification evidence

This matrix is the traceability record for task 13.8. Every requirement and its scenarios are covered by the listed implementation tasks plus automated or documented evidence. A row that names a real-environment gate is not release-complete until that gate has produced successful runtime evidence.

Current release gates:

- The complete backend suite passes with `647 passed`, including a PostgreSQL-dialect regression proving user/admin audit appends do not emit `INSERT ... RETURNING` and therefore do not require audit SELECT visibility.
- Earlier frontend, type, build, lint, color, focused migration, and strict OpenSpec checks passed in the implementation session; the final gates below still require fresh runtime evidence after the latest backend fix.
- Real PostgreSQL fresh/legacy execution remains task 2.9 and is verified by `scripts/verify_postgres_migrations.py` against disposable `lumenx_release_fresh` and `lumenx_release_legacy` databases.
- Real Chromium workflow remains task 13.3 and must cover administrator login through normal-user denial without committing disposable browser code.
- Task 13.8 remains open until tasks 2.9 and 13.3 have successful evidence and this matrix is re-reviewed.

### System administrator console

| Requirement | Tasks | Evidence |
| --- | --- | --- |
| Administrator-only console boundary | 1.3-1.5, 8.3, 12.1, 14.2, 14.5, 14.7-14.8 | `tests/test_admin_access.py`, `tests/test_admin_identity.py`, `tests/test_platform_administration_api.py`, `frontend/src/components/admin/__tests__/AdminAuthGate.spec.tsx`; real Chromium task 13.3 |
| Independent administrator identity and session lifecycle | 14.1-14.5, 14.7-14.8 | `tests/test_admin_identity.py`, `tests/test_database_context.py`, `tests/test_migration_chain.py`, `tests/test_audit.py`; PostgreSQL verifier covers user audit insert/admin-only read behavior |
| Idempotent sole-administrator bootstrap | 14.6, 14.8-14.10 | `tests/test_bootstrap_admin.py`, `tests/test_deployment_composition.py`, `tests/test_migration_chain.py`, `migrations/versions/0018_admin_recovery_rls.py`; `docs/system-admin-console.md` |
| Legacy shared administrator authority retirement | 2.9, 14.4, 14.8 | `tests/test_migration_chain.py`, `scripts/verify_postgres_migrations.py`; `docs/cloud-operations.md`; real PostgreSQL task 2.9 |
| Protection of the sole administrator | 9.3, 14.6, 14.8 | `tests/test_bootstrap_admin.py`, `tests/test_admin_contracts.py` |
| Dedicated administrator shell | 8.1-8.3, 8.7, 14.7 | `frontend/src/__tests__/admin-route.test.ts`, `frontend/src/components/admin/__tests__/PlatformAdminPage.spec.tsx`, `frontend/src/components/admin/__tests__/AdminAuthGate.spec.tsx`; real Chromium task 13.3 |
| Operational dashboard | 7.1-7.2, 8.4 | `tests/test_admin_contracts.py`, `tests/test_admin_inspection.py`, `frontend/src/components/admin/__tests__/PlatformAdminPage.spec.tsx` |
| Consistent administrator list behavior | 1.2, 7.2-7.3, 8.6 | `tests/test_admin_contracts.py`, `tests/test_admin_inspection.py`, `tests/test_platform_administration_api.py`, `frontend/src/__tests__/admin-route.test.ts` |
| Bounded administrator exports | 7.4-7.6 | `tests/test_admin_inspection.py` |
| Administrator action feedback | 4.3, 8.5, 12.5-12.6 | `frontend/src/components/admin/__tests__/PlatformAdminPage.spec.tsx`, `frontend/src/components/admin/__tests__/ManualRechargeAdminPage.spec.tsx`, `frontend/src/components/admin/__tests__/SystemScenesAdminPage.spec.tsx`, `tests/test_error_protocol.py`, `tests/test_admin_contracts.py` |
| Fail-closed console availability | 1.6, 4.7, 6.10, 12.1 | `tests/test_admin_access.py`, `tests/test_feature_flags.py`, `tests/test_admin_inspection.py`, `tests/test_manual_recharge.py` |

### Administrator user operations

| Requirement | Tasks | Evidence |
| --- | --- | --- |
| Administrator user directory | 7.3, 9.1 | `tests/test_platform_administration_api.py`, `frontend/src/components/admin/__tests__/PlatformAdminPage.spec.tsx` |
| Atomic administrator user creation | 3.8, 9.2 | `tests/test_platform_administration_api.py`, `tests/test_user_administration.py`, `frontend/src/components/admin/__tests__/PlatformAdminPage.spec.tsx` |
| Consolidated administrator user detail | 6.2-6.3, 9.4-9.5 | `tests/test_admin_inspection.py`, `frontend/src/components/admin/__tests__/UserDetailAdminPage.spec.tsx`; real Chromium task 13.3 |
| User security operations | 9.2 | `tests/test_authentication_service.py`, `tests/test_platform_administration_api.py`, `frontend/src/components/admin/__tests__/PlatformAdminPage.spec.tsx` |
| User-centric operational filtering | 6.1, 6.3, 9.6 | `tests/test_admin_inspection.py` |
| Administrator user-action audit | 9.2, 12.2 | `tests/test_platform_administration_api.py`, `tests/test_user_administration.py` |

### Manual recharge orders

| Requirement | Tasks | Evidence |
| --- | --- | --- |
| Manual recharge order identity | 2.1-2.3, 3.1-3.3 | `tests/test_manual_recharge.py`, `tests/test_migration_chain.py`; real PostgreSQL task 2.9 |
| Order and adjustment separation | 3.8, 10.6 | `tests/test_manual_recharge.py`, `tests/test_ticket_administration.py`, `tests/test_admin_contracts.py` |
| Controlled order state machine | 3.2, 3.4-3.7, 3.10 | `tests/test_manual_recharge.py`, `frontend/src/components/admin/__tests__/ManualRechargeAdminPage.spec.tsx` |
| Atomic wallet posting | 3.4, 3.10-3.11 | `tests/test_manual_recharge.py`, `scripts/verify_postgres_migrations.py`; real PostgreSQL task 2.9 |
| Idempotent order commands | 3.7, 3.10-3.11, 4.2 | `tests/test_manual_recharge.py`, `scripts/verify_postgres_migrations.py` |
| Pending-order cancellation | 3.5, 4.1-4.2, 10.4 | `tests/test_manual_recharge.py` |
| Manual order refunds | 3.6, 3.10-3.11, 10.5 | `tests/test_manual_recharge.py`, `frontend/src/components/admin/__tests__/ManualRechargeAdminPage.spec.tsx`; real Chromium task 13.3 |
| Immutable order event history | 2.1, 3.3-3.6, 4.4, 10.3 | `tests/test_manual_recharge.py`, `tests/test_migration_chain.py` |
| Order queries and exports | 4.1, 7.4-7.6, 10.1 | `tests/test_manual_recharge.py`, `tests/test_admin_inspection.py` |
| Manual-order reconciliation | 4.5-4.7, 10.7 | `tests/test_manual_recharge.py`, `frontend/src/components/admin/__tests__/ManualRechargeAdminPage.spec.tsx` |
| No external payment implication | 4.3, 10.3, 12.7 | `tests/test_admin_contracts.py`, `frontend/src/components/admin/__tests__/ManualRechargeAdminPage.spec.tsx`; `docs/system-admin-console.md` |

### System scene catalog

| Requirement | Tasks | Evidence |
| --- | --- | --- |
| Platform-owned scene templates | 2.4-2.6, 5.1-5.3 | `tests/test_system_scenes.py`, `tests/test_migration_chain.py` |
| Validated scene schema | 5.1, 5.10, 11.2 | `tests/test_admin_contracts.py`, `tests/test_system_scenes.py`, `frontend/src/components/admin/__tests__/SystemScenesAdminPage.spec.tsx` |
| Versioned scene editing | 5.2-5.3, 11.2 | `tests/test_system_scenes.py`, `frontend/src/components/admin/__tests__/SystemScenesAdminPage.spec.tsx` |
| Scene visibility and archival | 5.2, 5.8, 11.3 | `tests/test_system_scenes.py`, `frontend/src/components/admin/__tests__/SystemScenesAdminPage.spec.tsx` |
| System scene media | 5.4-5.5, 11.4 | `tests/test_system_scenes.py`, `tests/test_media_api.py` |
| Read-only user catalog | 5.5-5.6, 11.5 | `tests/test_system_scenes.py`, `frontend/src/components/library/__tests__/AssetLibrarySystemScenes.spec.tsx` |
| Snapshot copy into user scope | 5.7, 11.5 | `tests/test_system_scenes.py`, asset-library component test; real Chromium task 13.3 |
| System scene administrator directory | 5.2, 11.1 | `tests/test_system_scenes.py`, `frontend/src/components/admin/__tests__/SystemScenesAdminPage.spec.tsx` |
| Scene usage protection | 5.8, 11.3 | `tests/test_system_scenes.py`, `frontend/src/components/admin/__tests__/SystemScenesAdminPage.spec.tsx` |

### Administrator resource oversight

| Requirement | Tasks | Evidence |
| --- | --- | --- |
| Explicit administrator target scope | 6.1, 6.10 | `tests/test_admin_inspection.py` |
| Read-only resource inspection | 6.1-6.3, 6.10, 9.8 | `tests/test_admin_inspection.py`, `frontend/src/components/admin/__tests__/UserDetailAdminPage.spec.tsx` |
| Safe resource summaries | 6.4, 9.8 | `tests/test_admin_inspection.py` |
| Purpose-bound sensitive content access | 6.5, 6.10, 9.7-9.8 | `tests/test_admin_inspection.py`, `frontend/src/components/admin/__tests__/UserDetailAdminPage.spec.tsx`; real Chromium task 13.3 |
| Authorized administrator media preview | 6.6, 6.10, 9.7-9.8 | `tests/test_admin_inspection.py`, `tests/test_media_api.py` |
| Administrator AI task detail | 6.7, 9.5 | `tests/test_admin_inspection.py` |
| Operational exception queues | 6.8, 7.1, 8.4 | `tests/test_admin_inspection.py`, `frontend/src/components/admin/__tests__/PlatformAdminPage.spec.tsx` |
| Resource query boundaries | 6.3, 6.10, 7.2 | `tests/test_admin_inspection.py`, `scripts/verify_postgres_migrations.py` |
| No administrator impersonation | 12.7, 14.5 | `tests/test_admin_contracts.py`, `tests/test_admin_access.py` |
| Inspection audit search | 6.9, 9.5 | `tests/test_admin_inspection.py` |

### Ticket accounting

| Requirement | Tasks | Evidence |
| --- | --- | --- |
| Immutable ledger | 2.3, 3.4, 3.6, 3.10-3.11 | `tests/test_manual_recharge.py`, `tests/test_ticket_wallet.py`, `tests/test_migration_chain.py` |
| Manual administrator balance adjustment | 3.8, 10.6, 12.2 | `tests/test_ticket_administration.py` |
| User usage history | 3.9 | `tests/test_ticket_history.py`; real Chromium task 13.3 |
| Reconciliation invariants | 4.5-4.7, 10.7 | `tests/test_manual_recharge.py`, `tests/test_ticket_reconciliation.py` |

### Workspace data isolation

| Requirement | Tasks | Evidence |
| --- | --- | --- |
| Explicit audited administrator inspection path | 1.3-1.5, 6.1, 6.5-6.6, 6.10 | `tests/test_admin_access.py`, `tests/test_admin_inspection.py` |
| Defense-in-depth database isolation | 2.7-2.9, 6.1, 6.10, 13.2 | `tests/test_database_context.py`, `tests/test_adversarial_e2e.py`, `scripts/verify_postgres_migrations.py`; real PostgreSQL task 2.9 |
| Workspace asset scopes | 2.4-2.5, 5.2, 5.6-5.7 | `tests/test_system_scenes.py`, `tests/test_asset_api.py` |
| Private media namespace | 2.4, 5.4, 6.6 | `tests/test_system_scenes.py`, `tests/test_media_api.py` |
| Authorized media delivery | 5.5, 6.6, 9.7 | `tests/test_media_api.py`, `tests/test_system_scenes.py`, `tests/test_admin_inspection.py`, `tests/test_adversarial_e2e.py` |
