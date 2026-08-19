## MODIFIED Requirements

### Requirement: Immutable ledger
Every grant, paid manual recharge, recharge refund, hold, settlement, release, administrator adjustment, and compensation MUST append an immutable ledger record correlated to the wallet and relevant task, recharge order/event, or administrative action. New privileged entries MUST correlate to the independent administrator ID rather than a normal-user actor ID. Historical ledger records MUST NOT be updated or deleted.

#### Scenario: Charge needs correction
- **WHEN** an administrator confirms that a prior charge should be refunded
- **THEN** the system appends a compensating credit entry linked to the original charge rather than modifying it

#### Scenario: Manual recharge completes
- **WHEN** a pending manual recharge order is completed
- **THEN** exactly one recharge credit ledger entry is appended with the order and completion-event identifiers

#### Scenario: Manual recharge is refunded
- **WHEN** an accepted order refund debits tickets
- **THEN** a new refund ledger entry is appended with the order, refund event, independent administrator ID, reason, and post-transaction balances while the original recharge credit remains unchanged

### Requirement: Manual administrator balance adjustment
An independently authenticated system administrator SHALL be able to grant, compensate, or subtract tickets with a required Chinese reason, subject to nonnegative-balance rules, and every adjustment MUST be audited under the independent administrator ID. These balance adjustments MUST remain distinct from paid manual recharge orders and MUST NOT be reported as cash revenue.

#### Scenario: Administrator grants tickets
- **WHEN** an administrator grants a valid positive amount with a reason
- **THEN** the wallet and append-only grant ledger entry update atomically, the user can see the credit, and no paid recharge order is created

#### Scenario: Administrator records compensation
- **WHEN** an administrator compensates a user for a confirmed issue
- **THEN** the wallet and compensation ledger entry update atomically with the required audit correlation and no paid revenue is recorded

#### Scenario: Administrator attempts excessive debit
- **WHEN** an adjustment would make available balance negative or consume held tickets
- **THEN** the system rejects the adjustment

### Requirement: User usage history
An authenticated user SHALL be able to view available tickets, held tickets, and paginated transaction/usage history for that user only, including clearly distinguished paid manual recharge, recharge refund, grant, compensation, hold, consumption, and release entries.

#### Scenario: User opens consumption history
- **WHEN** an authenticated user requests wallet history
- **THEN** each visible item contains Chinese operation name, workspace/project or safe order context, metering tokens when applicable, ticket amount, status, and time without provider secrets, administrator private notes, or internal cost margins

#### Scenario: User views a manual recharge credit
- **WHEN** wallet history contains a completed manual recharge order
- **THEN** the entry is labeled as manual recharge and shows its safe order number and ticket amount without exposing administrator-only offline evidence

### Requirement: Reconciliation invariants
The system SHALL provide an administrator reconciliation process that verifies wallet caches, open holds, task terminal states, usage events, ledger sums, manual recharge order states, order events, and recharge/refund ledger correlations without silently rewriting history.

#### Scenario: Stale hold is detected
- **WHEN** reconciliation finds a hold attached to a terminal task
- **THEN** it reports the invariant violation and performs only an explicitly audited release or settlement action

#### Scenario: Recharge order and ledger diverge
- **WHEN** a completed or refunded manual recharge order does not match its immutable events and ledger deltas
- **THEN** reconciliation reports the affected order, user, wallet, expected values, and actual values without automatically changing financial history
