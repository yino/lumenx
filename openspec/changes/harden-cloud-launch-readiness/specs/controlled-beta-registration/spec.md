## ADDED Requirements

### Requirement: Controlled registration modes
Cloud registration MUST use one active mode: `disabled`, `invite_only`, or reserved `verified_open`. The system MUST NOT support unrestricted unverified phone registration.

#### Scenario: Registration is disabled
- **WHEN** a visitor submits registration while the active mode is `disabled`
- **THEN** the server returns a stable Chinese disabled response and creates no account or invitation mutation

#### Scenario: Verified open mode is activated without SMS
- **WHEN** an administrator attempts to activate `verified_open` while no verification provider is operational
- **THEN** configuration activation is rejected

### Requirement: Phone-bound invitations
A platform administrator SHALL be able to issue an expiring, revocable, single-use invitation bound to one canonical phone number, and only a non-reversible hash of the invitation secret SHALL be persisted.

#### Scenario: Administrator creates invitation
- **WHEN** an administrator submits a valid phone, expiry, and Chinese reason
- **THEN** the system returns the plaintext invitation once, stores its hash and canonical phone, and appends an audit event

#### Scenario: Invitation is revoked
- **WHEN** an administrator revokes an unused invitation with a reason
- **THEN** later registration attempts with that invitation are rejected without disclosing whether the phone is already registered

### Requirement: Atomic invitation registration
In `invite_only` mode, invitation validation, user creation, wallet/grant creation, default workspace creation, first session creation, and invitation consumption MUST commit atomically.

#### Scenario: Matching invitation registers successfully
- **WHEN** an unregistered canonical phone submits its valid unexpired invitation and compliant password
- **THEN** exactly one user boundary is created and the invitation is marked consumed by that user in the same transaction

#### Scenario: Registration transaction fails
- **WHEN** any dependent registration write fails
- **THEN** no user boundary is committed and the invitation remains unused for a safe retry

#### Scenario: Invitation is submitted twice
- **WHEN** concurrent requests use the same invitation
- **THEN** at most one registration succeeds and the other receives a nondisclosing conflict response

### Requirement: Invitation mismatch protection
Registration MUST normalize the submitted phone server-side and MUST reject an invitation bound to another canonical phone without revealing the invitation target.

#### Scenario: Invitation phone does not match
- **WHEN** a visitor submits a valid invitation with a different phone number
- **THEN** registration fails, no account is created, and the response does not reveal the expected phone

### Requirement: Registration policy discovery
The cloud frontend SHALL obtain a safe public registration-policy projection and SHALL show registration or invitation input only when allowed by the active mode.

#### Scenario: Visitor opens login page in invite mode
- **WHEN** the public registration policy reports `invite_only`
- **THEN** the Chinese registration form requires an invitation code and explains that the phone is not yet SMS-verified

#### Scenario: Visitor opens login page while disabled
- **WHEN** the policy reports `disabled`
- **THEN** the frontend hides or disables account creation while preserving login for existing users

### Requirement: Invitation abuse controls
Invitation creation, revocation, mismatch, expiry, replay, and consumption MUST be rate-limited or audited as appropriate without logging plaintext invitation values or raw passwords.

#### Scenario: Repeated invalid invitations are submitted
- **WHEN** a phone or network source exceeds the configured invitation attempt limit
- **THEN** further attempts are temporarily rejected with a stable Chinese rate-limit response and hashed diagnostic identifiers

### Requirement: Fail-closed legacy migration
Existing boolean registration configuration MUST migrate so disabled remains `disabled` and enabled becomes `invite_only`; migration MUST NOT produce unrestricted unverified registration.

#### Scenario: Previously enabled configuration is upgraded
- **WHEN** a deployment containing `registration_enabled=true` applies the migration
- **THEN** the effective mode is `invite_only` and registration remains unavailable until an administrator issues an invitation
