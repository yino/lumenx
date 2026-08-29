## ADDED Requirements

### Requirement: Prompt compilation is model-profile driven
The system SHALL compile a provider-neutral `ShotPackage` into a model-specific prompt and parameter payload using a Profile resolved from the active model catalog and route configuration.

#### Scenario: Compile a supported R2V shot
- **WHEN** a valid R2V `ShotPackage` targets an active model Profile
- **THEN** the compiler returns the rendered prompt, normalized generation mode, provider parameters, and ordered input references required by that Profile

#### Scenario: Unknown or inactive profile
- **WHEN** a requested Profile is not present in the model catalog or has no active database route
- **THEN** compilation fails with a model-route error and does not produce a submit-ready payload

### Requirement: Profile compilation validates capability constraints
The system SHALL validate generation mode, duration, resolution, reference count, audio options, and other declared parameters against the active model Profile before submission.

#### Scenario: Reference count exceeds profile limit
- **WHEN** a shot contains more reference images than the selected Profile allows
- **THEN** the compiler returns a blocking finding identifying the limit and no video task is created

#### Scenario: Unsupported audio option
- **WHEN** a shot requests provider-generated audio for a Profile that declares audio unsupported
- **THEN** the compiler either maps the request to the configured post-production audio path or returns an explicit warning/blocking result according to policy, without sending an unsupported provider parameter

### Requirement: Agent model selection is server-authoritative
The system SHALL treat Agent model recommendations as untrusted input and SHALL resolve the final model through the existing server-side model routing allowlist and active configuration snapshot.

#### Scenario: Client submits a hidden model
- **WHEN** a client or Agent proposes a model that is hidden, deprecated, or not enabled for the requested capability
- **THEN** the routing layer rejects the selection or applies the configured visible fallback and records the decision

#### Scenario: Route snapshot is captured
- **WHEN** a Profile is accepted for a billable run
- **THEN** the run stores the active route/provider/model snapshot and later retries use that snapshot rather than re-reading mutable client input

### Requirement: Compiled prompts preserve reference semantics
The system SHALL preserve a human-readable mapping between each ordered reference input and its semantic role, even when the provider transport uses positional fields such as `images` or `ref_image_urls`.

#### Scenario: Multiple reference images are compiled
- **WHEN** a shot uses a character image, scene image, and prop image
- **THEN** the compiled prompt or structured request records which ordered input serves each role and the provider payload preserves the same order

#### Scenario: Editor tags are removed for transport
- **WHEN** an internal editor tag is stripped from the plain prompt before provider submission
- **THEN** its semantic role remains available in the compiled reference map and task audit payload
