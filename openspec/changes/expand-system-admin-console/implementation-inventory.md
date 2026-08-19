## Existing implementation inventory

This change extends the current cloud control plane rather than replacing it.

### Backend components retained and extended

- `src/platform/administration_api.py`: existing paginated user, task, usage, audit, import-batch, and administrator-created-user endpoints.
- `src/platform/auth/api.py` and `src/platform/auth/admin.py`: normal-user session authentication, user suspension/reactivation, session revocation, reset credential, invitation, and atomic user-creation services. These services no longer grant administrator authority.
- `src/platform/ticket_administration*.py`: existing gift, compensation, and debit workflow. These remain non-revenue adjustments.
- `src/platform/configuration_*.py`: existing database-owned model routing and platform configuration.
- `src/platform/import_api.py` and `src/platform/observability_api.py`: existing import and metrics operations.
- `src/platform/db_models.py`, `src/platform/database.py`, and Alembic migrations: existing integer identifiers, no-foreign-key rule, transaction-local PostgreSQL RLS context, wallet/ledger/task/assets/media/audit records.
- `src/platform/asset_*.py` and `src/platform/media_*.py`: existing system asset scope, private media storage, and user workspace ownership checks.

### Frontend components retained and extended

- `frontend/src/components/admin/PlatformAdminPage.tsx`: existing administrator sections and user/task/usage/audit views.
- `frontend/src/components/admin/{Ticket,Configuration,Invitation,Import}AdminPage.tsx`: existing domain screens.
- `frontend/src/lib/adminRoute.ts`: existing `/admin` deep-link parser.
- `frontend/src/lib/api.ts`: existing typed administrator clients.
- `frontend/src/components/auth/AuthScreen.tsx` and `frontend/src/store/authStore.ts`: existing normal-user session gate and profile state; the administrator route bypasses both.

### Components added by this change

- Independent `admin_users`/`admin_sessions` identity, authentication, cookies, CSRF state, rate limits, route gate, and transaction context.
- Idempotent sole-administrator bootstrap and explicit credential recovery without creating normal-user state.
- Manual recharge order/event/reconciliation domain and APIs.
- System scene administration and authenticated read-only catalog/copy APIs.
- Dedicated read-only cross-user inspection, dashboard, exception, and export APIs.
- Dedicated grouped administrator shell, user detail, recharge orders, and system-scene screens.

### Components explicitly not replaced or introduced

- No external identity provider, payment provider, administrator impersonation, RBAC matrix, duplicate wallet, duplicate user-aggregate table, or duplicate scene table.
- Normal-user and administrator authentication are intentionally independent domains in the same deployment; neither session can authorize the other API family.
- Existing normal-user repositories keep their ownership signatures and RLS behavior.
