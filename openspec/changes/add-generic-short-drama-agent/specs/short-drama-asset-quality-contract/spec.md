## ADDED Requirements

### Requirement: Asset references have a canonical semantic contract
The system SHALL represent every referenced image, video, audio item, or text item with a stable alias, type, semantic purpose, source, authorization status, and resolved application media or resource identifier when required for execution.

#### Scenario: Bind an authorized character image
- **WHEN** a shot references a character image and the matching application asset has an authorized media record
- **THEN** the binder returns an ordered reference with the character role and media ID suitable for the Cloud AI Gateway

#### Scenario: Unresolved or unauthorized reference
- **WHEN** a prompt contains a reference alias that cannot be mapped to an authorized application asset
- **THEN** the binder reports a blocking finding naming the alias and does not synthesize a guessed URL or silently drop the reference

### Requirement: Generic and legacy reference syntax are normalized
The system SHALL accept the generic Skill syntax such as `@图片1` and normalize it to one canonical internal reference representation; existing editor tags such as `[characterN:name]` SHALL remain readable during migration.

#### Scenario: Import a generic Skill prompt
- **WHEN** an Agent receives a prompt containing `@图片1` and a matching material table
- **THEN** it creates a canonical reference with deterministic slot order and preserves the material's semantic role

#### Scenario: Mixed or duplicate references
- **WHEN** a prompt repeats the same reference or mixes legacy and generic syntax
- **THEN** normalization deduplicates by canonical identity while retaining the first explicit slot and reports ambiguous conflicting slots as warnings or blockers

### Requirement: Quality gate validates timeline and camera structure
The system SHALL validate that timeline segments are contiguous and duration-closed, references are present and typed, and mutually conflicting camera/edit instructions are reported before submission.

#### Scenario: Valid timed shot
- **WHEN** a shot's timeline starts at zero, has no gaps or overlaps, and ends at the declared duration
- **THEN** the timeline check passes and the shot can continue to model-specific validation

#### Scenario: Invalid timed shot
- **WHEN** a shot contains an overlap, gap, non-positive interval, or duration mismatch
- **THEN** the quality report contains a blocking finding and the submit stage is unavailable

### Requirement: Quality results use stable severity levels
The system SHALL classify findings as `blocking`, `warning`, or `suggestion`, include a machine-readable code and user-facing explanation, and record the validator that produced each finding.

#### Scenario: Warning-only package
- **WHEN** all findings are warnings or suggestions and the configured policy allows approval override
- **THEN** the package is marked `needs_approval` rather than silently marked production-ready

#### Scenario: Safety or rights blocker
- **WHEN** a shot contains an unconfirmed real-person right, dangerous action requiring review, or prohibited content signal
- **THEN** the package is marked `blocked` and no provider task is submitted

### Requirement: Batch runs maintain a continuity ledger
The system SHALL maintain a batch-level continuity ledger for character appearance, positions and axis, prop state, scene lighting, camera handoff, sound state, and locked references across adjacent shots.

#### Scenario: Adjacent shots share a character
- **WHEN** two consecutive shots reference the same character and the second shot is compiled
- **THEN** the continuity check compares the second shot against the prior ending state and reports any unapproved appearance, direction, or prop-state change

#### Scenario: Intentional continuity change
- **WHEN** the user explicitly records a costume, lighting, or axis change at a shot boundary
- **THEN** the ledger records the change as intentional and does not treat it as an unexplained continuity violation
