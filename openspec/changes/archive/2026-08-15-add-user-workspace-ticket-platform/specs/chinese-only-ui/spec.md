## ADDED Requirements

### Requirement: Fixed Chinese runtime locale
The user-visible application SHALL run in Chinese and SHALL NOT offer an English language mode or language selector.

#### Scenario: Application starts with prior English preference
- **WHEN** browser storage contains the legacy English locale
- **THEN** the application ignores or migrates it to Chinese and renders Chinese UI

#### Scenario: User opens settings
- **WHEN** the settings page is displayed
- **THEN** no language-selection control is present

### Requirement: Chinese visible copy
All product-authored text rendered to users or platform administrators MUST be Chinese, including navigation, page headings, secondary titles, buttons, form labels, placeholders, helper text, statuses, validation, empty states, dialogs, toasts, onboarding, billing descriptions, and administrative controls.

#### Scenario: Existing bilingual header is rendered
- **WHEN** a page previously used an English eyebrow or English secondary title
- **THEN** the visible English chrome is removed or replaced by Chinese

#### Scenario: AI task fails
- **WHEN** a safe user-facing task error is displayed
- **THEN** its actionable message and status are Chinese

### Requirement: Chinese authentication and billing terminology
New authentication, workspace, usage, and billing screens SHALL use consistent approved Chinese terminology, and the user-visible term for `ticket` SHALL be `算力券`.

#### Scenario: User views wallet
- **WHEN** the wallet summary and ledger are rendered
- **THEN** balances, holds, charges, refunds, and adjustments use consistent Chinese labels and `算力券` terminology

### Requirement: Technical-name exception
The system SHALL preserve provider brands, model names and IDs, technical abbreviations, file formats, resolutions, user-authored content, and values whose translation would change technical meaning in their canonical form.

#### Scenario: Model audit information is displayed
- **WHEN** a task detail shows `Wan 2.7`, `R2V`, `1080p`, or a provider task ID
- **THEN** the canonical technical value remains intact while its surrounding explanation is Chinese

### Requirement: Internal-content exception
Chinese-only UI requirements MUST NOT translate source code identifiers, API or database field names, internal logs, hidden system prompts, provider request payloads, or internal model instructions solely for localization.

#### Scenario: Internal English prompt is required by a provider
- **WHEN** an AI adapter assembles an English prompt that is not rendered as product chrome
- **THEN** the prompt remains unchanged unless a separate generation-quality requirement changes it

### Requirement: Safe Chinese backend errors
Cloud APIs SHALL return stable machine-readable error codes and safe Chinese user messages for expected authentication, authorization, validation, billing, configuration, and task errors.

#### Scenario: Provider returns an English stack trace
- **WHEN** a provider or backend exception occurs
- **THEN** the browser receives a mapped Chinese message and correlation ID rather than the raw stack trace

### Requirement: Localization regression checks
The frontend verification suite SHALL detect unapproved product-authored English strings in rendered primary user and administrator workflows while allowing an explicit reviewed technical-name allowlist.

#### Scenario: New English button label is introduced
- **WHEN** a change renders an English command not present in the approved allowlist
- **THEN** localization verification fails before release

### Requirement: Chinese locale infrastructure
The application MAY retain its message-based localization infrastructure, but Chinese messages SHALL be authoritative and missing Chinese keys MUST NOT fall back to visible English.

#### Scenario: Chinese message key is missing
- **WHEN** a component requests a key without a Chinese value
- **THEN** tests fail or the runtime uses a safe Chinese missing-copy indicator rather than displaying the English catalog value
