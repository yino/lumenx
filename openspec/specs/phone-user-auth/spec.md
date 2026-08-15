# phone-user-auth Specification

## Purpose
Define phone-and-password registration, secure session handling, account lifecycle, and deferred phone verification for cloud users.

## Requirements

### Requirement: Phone and password registration
The cloud system SHALL allow a visitor to register with a server-normalized phone number and password, SHALL enforce uniqueness on the canonical phone number, and SHALL create the user, ticket wallet, configured initial grant, and default workspace atomically.

#### Scenario: Successful registration
- **WHEN** a visitor submits a valid unused phone number and an acceptable password
- **THEN** the system creates one active unverified user, one wallet, and one default workspace and starts an authenticated session

#### Scenario: Duplicate phone number
- **WHEN** a visitor submits a phone number that normalizes to an existing account phone number
- **THEN** the system rejects registration without creating any partial wallet or workspace records

#### Scenario: Invalid registration data
- **WHEN** a visitor submits an invalid phone number or a password that does not satisfy the configured policy
- **THEN** the system rejects registration with a safe Chinese validation message

### Requirement: Deferred phone verification
The system SHALL persist phone verification state separately from the phone login identifier, SHALL mark first-release registrations as unverified, and SHALL expose an internal verification-provider boundary without requiring SMS verification in the first release.

#### Scenario: First-release account remains unverified
- **WHEN** a user registers while SMS verification is disabled
- **THEN** `phone_verified_at` remains empty and all authorization decisions treat the number as unverified

#### Scenario: Verification capability is enabled later
- **WHEN** an administrator enables an implemented verification provider in a future release
- **THEN** verification can update the existing user's verification state without changing user or workspace ownership identifiers

### Requirement: Password protection
The system MUST hash passwords using Argon2id or a comparably maintained memory-hard algorithm and MUST never persist or log plaintext passwords or password reset credentials.

#### Scenario: Password is stored
- **WHEN** registration or password change succeeds
- **THEN** only a salted password hash and password-change metadata are stored

### Requirement: Secure cloud session
The cloud system SHALL authenticate users with revocable opaque server-side sessions delivered in Secure, HttpOnly, SameSite cookies, with absolute and idle expiration.

#### Scenario: Successful login
- **WHEN** an active user submits the correct canonical phone number and password
- **THEN** the server creates a session, sets the secure cookie, and returns the user's safe profile and default workspace

#### Scenario: Invalid login
- **WHEN** the phone number is unknown or the password is incorrect
- **THEN** the server returns the same generic Chinese authentication error and does not reveal which credential was incorrect

#### Scenario: Expired session
- **WHEN** a request presents an expired or revoked session token
- **THEN** the server rejects the request as unauthenticated and clears the unusable cookie

### Requirement: Session token confidentiality
The system MUST store only a cryptographic hash of each session token and MUST protect state-changing cookie-authenticated requests with same-origin and CSRF checks.

#### Scenario: Session database is inspected
- **WHEN** an operator or attacker reads an `auth_sessions` record
- **THEN** the stored token hash cannot be used directly as a browser session credential

#### Scenario: Cross-site state mutation is attempted
- **WHEN** a state-changing request lacks the required origin or CSRF proof
- **THEN** the server rejects it before executing business logic

### Requirement: Logout and revocation
The system SHALL support logout of the current session and platform-administrator revocation of all sessions for a user.

#### Scenario: User logs out
- **WHEN** an authenticated user logs out
- **THEN** the current session is revoked and its cookie is cleared

#### Scenario: Administrator revokes a user
- **WHEN** a platform administrator revokes all sessions for a user
- **THEN** every existing session for that user becomes unusable

### Requirement: Password maintenance without SMS recovery
An authenticated user SHALL be able to change the password after proving the current password. Until phone verification is implemented, the system SHALL NOT offer SMS self-service password recovery and SHALL allow a platform administrator to issue a short-lived single-use reset credential through an audited support action.

#### Scenario: Authenticated password change
- **WHEN** a user submits the correct current password and a valid new password
- **THEN** the password hash is replaced and other sessions are revoked according to the security policy

#### Scenario: Unverified user requests SMS recovery
- **WHEN** an unverified user attempts self-service SMS password recovery
- **THEN** the system does not send or accept a recovery code and shows the configured Chinese support guidance

#### Scenario: Administrator-assisted reset
- **WHEN** a platform administrator issues a reset credential with a required reason
- **THEN** the action is audited and the credential expires after one use or its configured lifetime

### Requirement: Account status enforcement
The system SHALL support active and suspended user states and MUST check account status on every authenticated cloud request.

#### Scenario: Suspended user makes a request
- **WHEN** a valid session belongs to a suspended user
- **THEN** the server denies access, prevents new AI work, and does not expose workspace data

### Requirement: Authentication abuse controls
Registration, login, and reset endpoints MUST enforce configurable rate limits and SHALL record security-relevant events without logging credentials.

#### Scenario: Repeated login failures
- **WHEN** attempts exceed the configured phone or source limit
- **THEN** the server temporarily rejects further attempts with a generic Chinese response and records the rate-limit event
