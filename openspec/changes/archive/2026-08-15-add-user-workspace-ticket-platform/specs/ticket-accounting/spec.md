## ADDED Requirements

### Requirement: User ticket wallet
The system SHALL maintain one ticket wallet per user with available, held, and total balance represented as integer microtickets, where 1 displayed ticket equals 1,000,000 microtickets.

#### Scenario: User wallet is created
- **WHEN** user registration commits
- **THEN** exactly one wallet is created with the configured initial grant, which defaults to zero when unset

### Requirement: Normalized metering tokens
Every enabled AI operation MUST have a bounded server-side formula that converts provider usage and accepted parameters into integer metering tokens.

#### Scenario: LLM operation completes
- **WHEN** the provider returns input and output token counts
- **THEN** the system computes metering tokens using the versioned input/output weights stored in the task snapshot

#### Scenario: Image or video operation completes
- **WHEN** an image or video generation reaches a billable provider outcome
- **THEN** the system computes metering tokens from the accepted operation, output count, resolution, duration, audio option, and versioned model formula

#### Scenario: Model lacks a bounded formula
- **WHEN** an administrator attempts to enable a model operation without a valid metering formula
- **THEN** configuration activation is rejected

### Requirement: Versioned ticket conversion
The system SHALL convert metering tokens to microtickets with the task's snapshotted `tokens_per_ticket` value using deterministic integer arithmetic, and configuration changes SHALL affect only new tasks.

#### Scenario: Usage is converted
- **WHEN** a task consumes `t` metering tokens under exchange rate `n`
- **THEN** the charged amount equals `ceil(t * 1,000,000 / n)` microtickets

#### Scenario: Exchange rate changes during execution
- **WHEN** an administrator activates a new exchange rate while a task is running
- **THEN** the running task settles with its original snapshot and later tasks use the new rate

### Requirement: Atomic quote and reservation
Before enqueueing an AI task, the system MUST atomically validate scope and parameters, calculate a maximum quote, lock the wallet, verify available funds, create a hold, append reservation history, and persist the task.

#### Scenario: Sufficient balance
- **WHEN** the user has at least the quoted available microtickets
- **THEN** the quote is held and the task becomes eligible for enqueue without reducing the combined available-plus-held total

#### Scenario: Insufficient balance
- **WHEN** available balance is lower than the maximum quote
- **THEN** the system rejects task creation before any provider request and creates no hold

#### Scenario: Concurrent reservations compete for balance
- **WHEN** two requests concurrently attempt to reserve more than the wallet can jointly cover
- **THEN** database locking permits only reservations backed by real available balance and the wallet never becomes negative

### Requirement: Idempotent charging
AI task creation MUST require an idempotency key unique per user, and retries with the same effective request MUST return the original task and hold rather than creating another charge.

#### Scenario: Client retries after timeout
- **WHEN** the client repeats a task-creation request with the same idempotency key
- **THEN** the system returns the original task without creating a second hold, ledger entry, or provider submission

#### Scenario: Idempotency key is reused for different input
- **WHEN** the client reuses an existing key with a materially different request fingerprint
- **THEN** the system rejects the conflict

### Requirement: Successful settlement
On a billable successful outcome, the system MUST atomically calculate actual usage, convert it with the snapshot, consume the corresponding hold, release any unused amount, append immutable ledger entries, and persist a usage event.

#### Scenario: Actual charge is lower than quote
- **WHEN** a successful task uses fewer microtickets than were held
- **THEN** the actual amount is debited and the difference becomes available again

#### Scenario: Actual usage reaches maximum quote
- **WHEN** a successful task consumes its bounded maximum
- **THEN** the entire hold is debited without creating a negative balance

### Requirement: Failure and cancellation release
The system SHALL release the full unused hold when an operation fails before a billable provider outcome or is successfully cancelled before provider billing.

#### Scenario: Provider rejects before billing
- **WHEN** a queued or running task fails without a billable provider result
- **THEN** the hold is released and a zero-charge usage outcome is recorded

#### Scenario: Cancellation is accepted before billing
- **WHEN** the user cancels and the provider confirms no billable generation occurred
- **THEN** the full hold is released

### Requirement: Provider-billed partial failure
If the provider produces a billable result but local download, upload, or post-processing subsequently fails, the system SHALL settle the provider-backed usage and mark the task and ledger correlation for administrator review.

#### Scenario: Output upload fails after provider success
- **WHEN** provider success and billable usage are persisted but OSS upload fails
- **THEN** the provider-backed amount is charged, the task is marked failed with support-review state, and no duplicate provider request is made automatically

### Requirement: Immutable ledger
Every grant, hold, settlement, release, administrator adjustment, and compensation MUST append an immutable ledger record correlated to the wallet and relevant task or administrative action. Historical ledger records MUST NOT be updated or deleted.

#### Scenario: Charge needs correction
- **WHEN** an administrator confirms that a prior charge should be refunded
- **THEN** the system appends a compensating credit entry linked to the original charge rather than modifying it

### Requirement: Manual administrator balance adjustment
A platform administrator SHALL be able to add or subtract tickets with a required Chinese reason, subject to nonnegative-balance rules, and every adjustment MUST be audited.

#### Scenario: Administrator grants tickets
- **WHEN** an administrator grants a valid positive amount with a reason
- **THEN** the wallet and append-only ledger update atomically and the user can see the credit

#### Scenario: Administrator attempts excessive debit
- **WHEN** an adjustment would make available balance negative
- **THEN** the system rejects the adjustment

### Requirement: User usage history
An authenticated user SHALL be able to view available tickets, held tickets, and paginated transaction/usage history for that user only.

#### Scenario: User opens consumption history
- **WHEN** an authenticated user requests wallet history
- **THEN** each visible item contains Chinese operation name, workspace/project context, metering tokens, ticket amount, status, and time without provider secrets or internal cost margins

### Requirement: Reconciliation invariants
The system SHALL provide an administrator reconciliation process that verifies wallet caches, open holds, task terminal states, usage events, and ledger sums without silently rewriting history.

#### Scenario: Stale hold is detected
- **WHEN** reconciliation finds a hold attached to a terminal task
- **THEN** it reports the invariant violation and performs only an explicitly audited release or settlement action

