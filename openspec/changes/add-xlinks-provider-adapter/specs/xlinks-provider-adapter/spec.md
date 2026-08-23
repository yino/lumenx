## ADDED Requirements

### Requirement: Xlinks is a first-class provider backend
The system SHALL register `xlinks` as a provider backend that can be selected for an eligible model capability without embedding Xlinks-specific behavior in character, scene, storyboard, or other generation workflows. The existing `gpt-image-2` model identity SHALL be reused rather than duplicated for each provider channel.

#### Scenario: Xlinks is selected for GPT Image text-to-image
- **WHEN** an `image.t2i` route for `gpt-image-2` explicitly selects the `xlinks` backend
- **THEN** the runtime constructs the Xlinks image adapter and sends the generation through Xlinks

#### Scenario: Cloud deployment switches its active image route
- **WHEN** an administrator activates the target Cloud deployment's server-owned `image.t2i` route for `gpt-image-2` on Xlinks after the contract gate passes
- **THEN** Cloud character text-to-image requests use Xlinks without requiring or accepting a browser model/provider override

#### Scenario: Non-PC model defaults are unchanged
- **WHEN** a consumer outside the PC/Cloud Web deployment resolves the catalog default
- **THEN** this PC-only change does not alter that consumer's existing provider selection

### Requirement: Provider selection is capability-aware
The runtime MUST validate backend eligibility against both model and capability. Xlinks SHALL be eligible only for `image.t2i` in the first release and MUST NOT receive image-editing or video requests.

#### Scenario: Character text-to-image uses Xlinks
- **WHEN** the PC character workbench submits a text-only generation
- **THEN** the server-owned T2I route uses Xlinks while retaining the existing character prompt, style suffix, aspect ratio, and output persistence behavior

#### Scenario: Image editing is requested
- **WHEN** a request with one or more reference images resolves `gpt-image-2` for `image.i2i`
- **THEN** the router uses an explicitly eligible non-Xlinks backend or rejects configuration before provider submission, and it does not send the request to Xlinks

#### Scenario: Future video model is configured prematurely
- **WHEN** an Xlinks-backed video capability has not been separately cataloged and implemented
- **THEN** the runtime rejects the route as unsupported instead of reusing the image adapter

### Requirement: Xlinks credentials and endpoint are isolated
The adapter MUST obtain the Xlinks API token from `XLINKS_API_KEY`, MUST use `XLINKS_BASE_URL` with a default of `https://api.xlinks.site/v1`, and MUST send the token only as `Authorization: Bearer <token>` to the configured Xlinks origin. Secrets MUST NOT appear in project data, task payloads, logs, exceptions, audit projections, or browser-readable cloud configuration.

#### Scenario: Cloud route references an Xlinks secret
- **WHEN** a cloud administrator activates an Xlinks route with an allowed Xlinks secret reference
- **THEN** the worker resolves that secret server-side and the browser receives no credential value

#### Scenario: Credential is missing
- **WHEN** an Xlinks request starts without a resolvable `XLINKS_API_KEY`
- **THEN** generation fails before any network request with a safe Chinese configuration error

### Requirement: GPT Image 2 T2I request mapping
The Xlinks image adapter SHALL call `POST /images/generations` relative to `XLINKS_BASE_URL` using JSON and model ID `gpt-image-2`. It MUST require a non-empty prompt, normalize LumenX aspect-ratio sizes to an Xlinks-supported size, constrain quality to `auto`, `low`, `medium`, or `high`, use `png` output for the first release, and send exactly one requested output per provider invocation.

#### Scenario: Portrait character generation
- **WHEN** LumenX requests a portrait T2I size such as `576*1024`
- **THEN** the adapter sends `1024x1536`, `n: 1`, and `output_format: "png"` with the original prompt

#### Scenario: Landscape scene generation
- **WHEN** LumenX requests a landscape T2I size not natively accepted by Xlinks
- **THEN** the adapter normalizes it to `1536x1024` without changing the requested orientation

#### Scenario: Square prop generation
- **WHEN** LumenX requests a square T2I image
- **THEN** the adapter sends `1024x1024`

#### Scenario: Unsupported provider parameter is supplied
- **WHEN** an Xlinks route receives an unsupported value or an explicit unsupported field such as a negative prompt
- **THEN** server-side validation rejects it or the adapter omits non-provider metadata according to the catalog contract, and no undocumented field is forwarded

### Requirement: Xlinks results are safely materialized
The adapter MUST accept a successful OpenAI-compatible image response containing either `data[].b64_json` or `data[].url`, validate that exactly one usable image is available, enforce bounded download and decode limits, and write the result only to the caller-provided output path. The adapter MUST reject HTML, malformed base64, empty output, unsafe redirects, and non-image payloads.

#### Scenario: Base64 image succeeds
- **WHEN** Xlinks returns one valid `b64_json` image
- **THEN** the adapter decodes it to the controlled output path and returns provider usage metadata without exposing the base64 payload

#### Scenario: URL image succeeds
- **WHEN** Xlinks returns one HTTPS image URL
- **THEN** the adapter downloads a bounded image response to the controlled output path and preserves the Xlinks request identifier

#### Scenario: Response has no usable image
- **WHEN** Xlinks returns success status without a valid URL or base64 image
- **THEN** the adapter reports a normalized provider-response failure and does not create a successful asset record

### Requirement: Submission and retry behavior is billing-safe
The adapter MUST distinguish connection/setup failures, explicit HTTP failures, and ambiguous timeouts after request transmission. It MUST NOT blindly retry an ambiguous image-generation POST unless Xlinks documents and the adapter supplies a supported idempotency mechanism. Automatic fallback MUST remain prohibited after a potentially billable provider acceptance.

#### Scenario: Xlinks rejects a request before generation
- **WHEN** Xlinks returns a documented nonbillable validation or authentication failure
- **THEN** the adapter returns a normalized nonretryable error with no secret-bearing diagnostics

#### Scenario: Rate limit is returned explicitly
- **WHEN** Xlinks returns HTTP 429 before a generation result
- **THEN** retry or fallback follows the configured gateway policy and records the provider attempt

#### Scenario: Read timeout occurs after submission
- **WHEN** the POST may have reached Xlinks but no definitive response is received
- **THEN** the task is marked ambiguous or support-review-required and the adapter does not submit a duplicate request automatically

### Requirement: Provider execution metadata is retained
The Xlinks adapter SHALL capture `x-oneapi-request-id` or an equivalent documented request identifier when present, pass it through the existing provider-submission callback, and include bounded non-secret usage metadata in the provider result.

#### Scenario: Response includes a request ID
- **WHEN** Xlinks returns `x-oneapi-request-id`
- **THEN** the task attempt persists the provider name `xlinks` and request ID for diagnostics and reconciliation

#### Scenario: Response has no request ID
- **WHEN** a synchronous successful response contains no provider identifier
- **THEN** the adapter still records a provider attempt with a null external ID and never invents one

### Requirement: Cloud character UI reflects the server-owned image route
The Cloud application SHALL keep image model/provider selection server-owned and MUST NOT expose an end-user provider selector or accept browser route overrides. It SHALL expose only a safe effective-engine projection containing presentation identity and UI-relevant capability flags, and the character workbench SHALL use that projection instead of the Cloud deployment mode itself to gate GPT Image-specific templates.

#### Scenario: Xlinks GPT Image route is active
- **WHEN** the effective Cloud `image.t2i` route is `gpt-image-2` on Xlinks and reports support for the character design-sheet template
- **THEN** the character workbench shows a read-only `GPT Image 2 · Xlinks` engine label and enables `角色设定图`

#### Scenario: Cloud user starts character generation
- **WHEN** a Cloud user generates a character with the effective Xlinks route
- **THEN** the browser request contains no provider ID, model override, credential, base URL, secret reference, or internal route ID, and the server resolves the active route authoritatively

#### Scenario: Effective route is ineligible or unavailable
- **WHEN** the effective route does not support the character design-sheet capability or its safe projection cannot be loaded
- **THEN** `角色设定图` remains locked with a non-secret availability explanation while generally supported character templates remain usable

#### Scenario: Administrator changes the active route
- **WHEN** the server-owned `image.t2i` route changes between providers or models
- **THEN** the workspace-scoped effective-engine projection is invalidated or refreshed before the next generation and server-side capability validation prevents stale UI state from selecting an unsupported template

#### Scenario: Browser reads effective engine metadata
- **WHEN** the Cloud browser requests the effective-engine projection
- **THEN** the response omits credentials, secret references, base URLs, internal route identifiers, pricing rules, and fallback policy

### Requirement: Documentation evidence gates activation
The repository SHALL capture the Xlinks source URL, capture date, confirmed authentication and endpoint facts, inferred facts, and unresolved image-contract questions. Production activation of the Xlinks T2I route MUST require a credentialed contract smoke test that verifies request acceptance, response shape, content type, request ID, and billing-safe failure behavior.

#### Scenario: Catalog is built before contract verification
- **WHEN** the Xlinks evidence still lacks a credentialed image response example
- **THEN** the catalog may contain backend metadata but the PC `image.t2i` route is not activated

#### Scenario: Contract smoke test passes
- **WHEN** a credentialed `gpt-image-2` T2I request confirms the documented adapter assumptions
- **THEN** the evidence records redacted results and the route may be enabled according to deployment policy
