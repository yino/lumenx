## ADDED Requirements

### Requirement: Manual recharge order identity
The system SHALL persist every administrator-recorded paid ticket purchase as a manual recharge order with an auto-increment integer ID, immutable unique order number, target user, `CNY` amount in integer fen, ticket amount in integer microtickets, configuration/exchange snapshot, optional offline reference, status, optimistic version, independent administrator actor metadata, and timestamps. A normal-user ID MUST NOT be used as the privileged order actor.

#### Scenario: Pending order is created
- **WHEN** the administrator submits a valid target user, positive cash amount, positive ticket amount, offline details, and reason
- **THEN** the system creates one `pending` order without changing the user's wallet

#### Scenario: Duplicate order number is attempted
- **WHEN** concurrent requests attempt to create the same generated or supplied unique order reference
- **THEN** at most one order is created and neither request credits the wallet

### Requirement: Order and adjustment separation
Paid manual recharge orders MUST remain distinct from administrator grants, compensation, and balance debits. Grant, compensation, or debit actions SHALL NOT create a paid order or report cash revenue.

#### Scenario: Administrator grants promotional tickets
- **WHEN** the administrator uses the gift action with a reason
- **THEN** the wallet and ledger record a grant and no manual recharge order is created

#### Scenario: Administrator records an offline purchase
- **WHEN** the administrator creates and completes a manual recharge order
- **THEN** finance reports classify the resulting credit as paid manual recharge rather than grant or compensation

### Requirement: Controlled order state machine
Manual recharge order transitions MUST follow `pending -> completed -> partially_refunded -> refunded` or `pending -> cancelled`, and every transition SHALL use optimistic version checking and row locking.

#### Scenario: Pending order is completed
- **WHEN** an administrator confirms offline receipt for the current pending version
- **THEN** the order becomes completed and records the completing administrator and timestamp

#### Scenario: Completed order is cancelled
- **WHEN** an administrator attempts to cancel an order that has already been completed
- **THEN** the server rejects the invalid transition and directs the administrator to the refund workflow

#### Scenario: Stale order version is submitted
- **WHEN** two administrator requests act on the same prior version
- **THEN** only one transition commits and the other returns a conflict with the current order state

### Requirement: Atomic wallet posting
Completing a manual recharge order MUST atomically lock the order and target wallet, append exactly one recharge ledger credit, append one immutable completion event, update lifetime credit totals, and mark the order completed.

#### Scenario: Recharge posting succeeds
- **WHEN** a valid pending order is completed
- **THEN** the full order ticket amount becomes available and the order, wallet, ledger, event, and audit records commit together

#### Scenario: Ledger append fails
- **WHEN** any required order completion record cannot be persisted
- **THEN** the transaction rolls back and both order status and wallet balance remain unchanged

### Requirement: Idempotent order commands
Order creation, completion, cancellation, and refund commands MUST require an administrator-scoped idempotency key and request fingerprint, and retries with the same effective command SHALL return the original result.

#### Scenario: Completion response is lost
- **WHEN** the administrator retries the same completion command after its transaction committed
- **THEN** the API returns the completed order without a second wallet credit, ledger entry, event, or audit mutation

#### Scenario: Key is reused for different values
- **WHEN** an idempotency key is reused with a different target user, order, amount, or action
- **THEN** the server rejects the conflict and performs no mutation

### Requirement: Pending-order cancellation
The administrator SHALL be able to cancel only a pending order with a mandatory reason; cancellation SHALL append an immutable order event and audit event without changing the wallet.

#### Scenario: Pending order is cancelled
- **WHEN** the administrator cancels a pending order at its current version with a reason
- **THEN** the order becomes cancelled, records actor and time, and creates no ticket ledger entry

### Requirement: Manual order refunds
The administrator SHALL be able to record one or more partial or full refunds against a completed order by supplying positive cash fen and microtickets, an idempotency key, and a reason. Cumulative refunded values MUST NOT exceed the original order values or make the available ticket balance negative.

#### Scenario: Partial refund succeeds
- **WHEN** requested cash and ticket amounts are within the order's unrefunded amounts and the wallet has enough available tickets
- **THEN** the system atomically debits the wallet, appends a refund ledger entry and event, updates refunded totals, and marks the order partially refunded

#### Scenario: Final refund succeeds
- **WHEN** a refund brings both cumulative cash and ticket refund totals to the original order amounts
- **THEN** the order becomes refunded and retains all original and refund history

#### Scenario: User has spent the refundable tickets
- **WHEN** the requested refund would make available tickets negative or would reclaim held tickets
- **THEN** the refund is rejected with the exact available-ticket shortfall and no order or wallet record changes

#### Scenario: Refund exceeds original order
- **WHEN** cumulative cash or ticket refunds would exceed the order snapshot
- **THEN** the request is rejected before any ledger or order event is appended

### Requirement: Immutable order event history
Every create, complete, cancel, and refund action SHALL append an immutable order event containing the order ID, target user ID, independent administrator ID, action, amount deltas, idempotency identity, linked ledger entry when applicable, reason, safe snapshot, and timestamp. Historical events MUST NOT be updated or deleted.

#### Scenario: Administrator opens order detail
- **WHEN** an administrator views an order
- **THEN** the console displays its current state and ordered immutable event timeline with ledger correlations

### Requirement: Order queries and exports
The console SHALL support paginated order search and CSV export by order number, user ID/phone, status, independent administrator ID, offline reference, and bounded creation/completion/refund date ranges.

#### Scenario: Administrator searches by user
- **WHEN** an administrator opens a user's order tab
- **THEN** only that user's orders are returned with cash, tickets, state, operator, timestamps, and ledger correlation

### Requirement: Manual-order reconciliation
The system SHALL provide a non-mutating reconciliation report that verifies order state, aggregate refunded amounts, immutable events, recharge/refund ledger entries, and wallet deltas without silently correcting discrepancies.

#### Scenario: Completed order lacks a credit
- **WHEN** reconciliation finds a completed order without exactly one matching recharge ledger credit
- **THEN** it reports a high-severity exception with order and wallet identifiers and performs no automatic credit

#### Scenario: Order history is consistent
- **WHEN** all state transitions, events, ledger entries, and amounts reconcile
- **THEN** the report marks the order consistent and includes it in aggregate totals exactly once

### Requirement: No external payment implication
The first-release order API and UI MUST identify these records as administrator-confirmed manual/offline orders and MUST NOT claim that a payment provider verified or settled funds.

#### Scenario: Completed manual order is displayed
- **WHEN** the administrator views a completed order
- **THEN** the console labels it as manually confirmed and shows the recording administrator and offline reference rather than a fabricated payment-channel transaction
