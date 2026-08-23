## Context

LumenX already catalogs `gpt-image-2` and implements it through `MuleRouterImageModel`. The browser-based PC deployment runs in Cloud mode, resolves a capability-specific database route, and constructs a request-scoped provider client. The provider registry and model catalog support multiple backends for a family, but `xlinks` is not currently an accepted backend or client factory.

Xlinks presents itself as an OpenAI-compatible gateway at `https://api.xlinks.site/v1`. Its public documentation confirms Bearer authentication with `sk-nexus-` tokens, `GET /v1/models`, standard error envelopes, and an OpenAI-compatible base URL. Unauthenticated probes on 2026-08-23 confirmed that `POST /v1/images/generations` and `POST /v1/images/edits` are routed endpoints because both return the same 401 token error as the documented chat endpoint. The public documentation does not describe image request or response bodies.

The authenticated Xlinks web workbench uses an internal portal API rather than the public API. Its client shows `gpt-image-2` T2I/I2I controls for `auto`, `1024x1024`, `1536x1024`, and `1024x1536`; qualities `auto/low/medium/high`; formats `png/jpeg/webp`; backgrounds `auto/opaque/transparent`; and one to four outputs. These controls are evidence of supported product options, not a sufficient public API contract.

The first implementation therefore needs both a provider adapter and an evidence gate. It must also respect the current single-output `ProviderGenerationResult` contract and avoid sending Xlinks requests from I2I/video call sites before those capabilities are specified.

The current browser-based PC deployment is built in Cloud mode. In that mode `CastWorkbenchModal` intentionally omits browser model controls, but it also hard-codes `isGptImage2` to false. As a result, the `角色设定图` template remains locked even when the server's effective `image.t2i` route uses `gpt-image-2`. The Cloud UI needs a safe description of the effective route for presentation and feature gating without regaining authority over provider selection.

## Goals / Non-Goals

**Goals:**

- Add Xlinks as a reusable, channel-owned provider backend.
- Execute every PC/Cloud Web `image.t2i` workflow through `gpt-image-2` on Xlinks.
- Preserve the existing logical model ID and MuleRouter behavior.
- Make backend resolution capability-aware so Xlinks receives only T2I in this release.
- Normalize parameters, provider errors, request IDs, and output media into existing LumenX contracts.
- Keep secrets server/local-process only and maintain cloud billing/recovery guarantees.
- Make Xlinks the active PC deployment `image.t2i` route while the browser remains provider-agnostic and cannot override the route.
- Show Cloud users a read-only effective engine identity and unlock GPT Image-specific character templates from server-reported capability metadata.
- Leave a clean construction point for later Xlinks image-editing and video adapters.

**Non-Goals:**

- Changing the desktop application, its model settings, or its packaging.
- Changing catalog defaults used by consumers outside the PC deployment.
- Adding an end-user model or provider selector to Cloud-mode character generation.
- Allowing Cloud requests to submit model IDs, provider IDs, credentials, base URLs, or route overrides from the browser.
- Implementing `POST /v1/images/edits` or accepting reference images through Xlinks.
- Implementing Sora or any other Xlinks video model.
- Supporting more than one provider output from a single invocation.
- Adding a general OpenAI SDK dependency; the adapter will use the repository's existing HTTP stack.
- Completing the external raw-doc archive or Context Hub promotion in this repo-only proposal.

## Decisions

### 1. Add a provider-owned `XlinksImageModel`

Create `src/models/xlinks.py` with a small shared Xlinks HTTP client layer and an `XlinksImageModel(ImageGenModel)` implementation. The adapter owns authentication, endpoint construction, request serialization, response parsing, output materialization, error classification, and provider metadata.

This keeps channel behavior out of `AssetGenerator`, `provider_runtime`, and model-specific workflows. A later `XlinksVideoModel` can reuse only the proven HTTP/auth/error primitives while keeping video submission and polling separate.

Alternative considered: add Xlinks branches to `MuleRouterImageModel`. Rejected because the providers have different credentials, endpoints, billing ambiguity, and future capability roadmaps.

### 2. Reuse `gpt-image-2` and add an Xlinks backend

Extend the existing `gpt-image` catalog family with backend `xlinks`, credential source `XLINKS_API_KEY`, runtime gateway metadata, and an Xlinks capability declaration limited to T2I. Preserve `mulerouter` as the catalog `default_backend` so this PC-only change does not alter other deployment modes.

PC routes remain capability-specific and select provider `xlinks` directly. After the contract gate passes, the target PC deployment activates its server-owned `image.t2i` route for `gpt-image-2` on Xlinks without changing the catalog's MuleRouter default. If Xlinks is requested for an ineligible capability, configuration fails before submission; the adapter never silently converts I2I into T2I.

Alternative considered: add a second model such as `xlinks-gpt-image-2`. Rejected because the provider channel is not a distinct model and duplicate IDs would fragment settings, history, and pricing metadata.

### 3. Use the public OpenAI-compatible endpoint, not portal APIs

The adapter uses `XLINKS_BASE_URL`, defaulting to `https://api.xlinks.site/v1`, and posts to `/images/generations` with `Authorization: Bearer`. It does not send the portal-only `Tenant-Id`, browser session token, `api_key_id`, or `/api/creation-tasks` form data.

The request body is intentionally conservative:

```json
{
  "model": "gpt-image-2",
  "prompt": "...",
  "n": 1,
  "size": "1024x1536",
  "quality": "high",
  "output_format": "png",
  "background": "auto"
}
```

Only parameters confirmed by Xlinks' workbench and compatible with the OpenAI image API are sent. `negative_prompt`, `seed`, `prompt_extend`, and provider-specific fields are not forwarded.

Alternative considered: use the authenticated portal creation-task endpoints. Rejected because they require browser-account state and an API-key database ID, are not documented as a service integration contract, and would bypass the user's API token boundary.

### 4. Normalize LumenX sizes and fix provider output count

Normalize square, landscape, and portrait inputs to `1024x1024`, `1536x1024`, and `1024x1536`. Reject ambiguous or malformed dimensions after applying the existing orientation-preserving fallback.

Every provider invocation sends `n=1`. The PC route validation caps Xlinks `count` at one until LumenX gains a multi-output provider result contract. This prevents silently discarding paid outputs.

The first release requests PNG only because cloud output persistence currently declares a `.png` suffix and `image/png`. JPEG/WebP can be exposed after the output contract becomes content-type aware.

### 5. Parse URL and base64 responses defensively

Because Xlinks does not document its image response example, accept the two standard OpenAI-compatible shapes: `data[].b64_json` and `data[].url`. Require exactly one usable entry, prefer base64 when both are present, and reject empty/malformed payloads.

For URL results, require HTTPS by default, cap redirects and bytes, require an image content type, stream to a temporary sibling file, validate the decoded image signature, and atomically replace the caller-provided output. Base64 decoding receives equivalent size and signature checks. Provider payloads and image bytes are never logged.

### 6. Treat ambiguous POST failures as potentially billable

Do not apply the generic retry loop to generation POSTs. Connection failures known to occur before transmission may be classified as unavailable, and explicit 429/5xx responses may follow configured retry/fallback policy only when the gateway can treat them as nonbillable. A read timeout or connection loss after transmission is ambiguous: persist the attempt/request ID if available, mark review-required, and do not resubmit automatically.

This is stricter than convenient automatic retries, but it complies with the existing server gateway and ticket-accounting rules when Xlinks offers no documented idempotency key or task lookup API.

### 7. Integrate the PC/Cloud construction path

PC work includes the catalog/provider registry, `RequestScopedModelClientFactory` builder registration, secret-reference validation/seeding, route configuration tests, and provider execution metadata. Desktop adapter selection, settings, environment dialogs, and packaging are intentionally unchanged.

The configuration surface adds:

- `XLINKS_API_KEY`
- `XLINKS_BASE_URL` (optional override)

No Xlinks secret is exposed through PC browser APIs.

### 8. Keep Cloud routing server-owned and project it safely to the character UI

Cloud-mode browsers do not receive a model/provider selector and do not submit model or provider overrides. The server resolves the effective `image.t2i` route and exposes a bounded presentation projection containing only the logical model ID, model display name, provider display name, and UI-relevant capability flags. It excludes credentials, secret references, base URLs, internal route IDs, pricing rules, and fallback policy.

`CastWorkbenchModal` uses that projection to show a read-only engine label such as `GPT Image 2 · Xlinks` near generation configuration. The existing Cloud-only `isGptImage2 = false` behavior is replaced with capability-driven gating: `角色设定图` unlocks when the effective route reports the GPT Image character-design-sheet capability and remains locked when the route is ineligible or the projection cannot be loaded. Generation requests remain unchanged and provider-neutral; the backend route is authoritative.

The safe projection must be scoped and cached consistently with the active workspace/deployment so a route switch cannot leave another tenant's engine metadata in browser state. Route changes invalidate or refresh the projection before the next generation. The UI fails closed for the specialized template but keeps generally supported templates usable.

Alternative considered: expose a MuleRouter/Xlinks segmented selector in the Cloud character modal. Rejected because Cloud model routing, credentials, billing, and fallback are server-owned, and the existing client sanitization intentionally strips provider/model overrides.

### 9. Gate activation on a credentialed contract fixture

Capture the public docs and redacted observations in `docs/api-reference/xlinks-gpt-image-2.md`. Add adapter tests from recorded/synthetic fixtures for URL, base64, errors, and timeouts. Before enabling an Xlinks route outside development, run one credentialed smoke test to verify the exact request, response, content type, request ID header, latency, and error behavior. Store no token or generated binary in source control.

## Risks / Trade-offs

- **[Undocumented public image schema]** The endpoint exists but its image contract is absent from public docs. → Parse only standard shapes and require a credentialed smoke test before PC activation.
- **[Duplicate billing after timeout]** A synchronous POST can be accepted even when the client times out. → Disable blind POST retries and mark ambiguous attempts for review.
- **[Capability leakage]** Model-prefix-only routing could send I2I to the T2I-only adapter. → Make backend eligibility capability-aware and test all character generation branches.
- **[Output format mismatch]** Xlinks can return JPEG/WebP while LumenX currently declares PNG. → Force PNG for this release.
- **[Single-output limitation]** Xlinks supports up to four outputs, but LumenX's provider result has one path. → Use `n=1` throughout the PC T2I path.
- **[Provider outage or contract drift]** Gateway behavior may change without versioned image docs. → Validate response shape strictly, preserve request IDs, and keep MuleRouter as rollback.
- **[Stale Cloud engine metadata]** The character UI could show or unlock behavior for an old route after an administrator switches providers. → Scope the safe projection to the active workspace/deployment, invalidate it on route changes, and revalidate capability server-side at submission.
- **[Configuration complexity]** Users can confuse model selection with provider selection. → Keep one model identity, keep Cloud routing server-owned, and show only a read-only effective engine label to Cloud users.

## Migration Plan

1. Capture Xlinks evidence and add contract fixtures without enabling runtime routing.
2. Add the adapter, provider/backend registry support, catalog metadata, generated artifacts, and tests.
3. Add PC server credential and factory plumbing while leaving non-PC catalog defaults unchanged.
4. Add the safe Cloud effective-engine projection and capability-driven character-template gating without adding browser route controls.
5. Configure Xlinks only in a development route and run the credentialed smoke test.
6. After the smoke test passes, activate the target Cloud deployment's server-owned `image.t2i` route on Xlinks and verify the browser shows `GPT Image 2 · Xlinks` with `角色设定图` unlocked.
7. Roll back by returning the active PC `image.t2i` route to the previous provider; no project data migration is required.

## Open Questions

- The credentialed smoke test returned exactly one `data[0].b64_json` PNG and honored `output_format: "png"`; URL responses remain supported defensively.
- The credentialed smoke test returned a request ID; longer-term stability for provider support/reconciliation remains undocumented.
- Does Xlinks support an idempotency header or a request-status lookup for synchronous image calls?
- Which failures are definitively nonbillable, and does a timeout after acceptance consume balance?
- Is usage or cost metadata returned in the image response, or must LumenX meter only from configured parameters?

The credentialed contract gate has passed for request acceptance and response parsing. Idempotency and exact billing semantics remain documented operational risks and do not permit automatic ambiguous retries.
