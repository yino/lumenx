## Why

LumenX currently exposes `gpt-image-2` only through the MuleRouter adapter, which couples that model to one channel and prevents use of an Xlinks API credential. Adding Xlinks as a first-class provider backend creates a channel-oriented integration boundary for `gpt-image-2` text-to-image now and for separately specified image or video capabilities later.

## What Changes

- Add an `xlinks` provider backend with its own credential, base URL, request/response adapter, error normalization, and execution metadata handling.
- Route every PC/Cloud Web `image.t2i` request to the existing logical `gpt-image-2` model on Xlinks; do not create a duplicate model identity.
- Map LumenX image-generation parameters to Xlinks' OpenAI-compatible `POST /v1/images/generations` contract, including prompt, size, quality, output format, and background. The PC route fixes provider `n=1`.
- Accept either URL or base64 image results, validate the returned media, and write it through the existing LumenX media/output boundary.
- Add catalog metadata, the Cloud route factory, and server credential plumbing for `XLINKS_API_KEY`, `XLINKS_BASE_URL`, and the Xlinks backend selection.
- Keep PC browser model/provider routing server-owned: activate the `image.t2i` route for `gpt-image-2` on Xlinks after contract verification, expose only a safe read-only engine label to the character workbench, and never accept a browser provider override.
- Make the character workbench derive `角色设定图` availability from the effective server route so a Cloud deployment backed by `gpt-image-2` can unlock the template instead of treating every Cloud session as non-GPT Image.
- Preserve the catalog's existing MuleRouter default for consumers outside the PC deployment, while making Xlinks the active PC `image.t2i` route after the credentialed contract smoke test passes.
- Capture the Xlinks documentation evidence locally and record the current limitation that the public docs describe authentication and OpenAI compatibility but omit image request/response examples.
- Keep Xlinks image editing, single-request multi-output, and all Xlinks video generation out of the first implementation. The adapter and provider registration SHALL allow those capabilities to be added without embedding Xlinks behavior in model-specific business workflows.
- Keep the desktop application, desktop settings, and desktop packaging out of this change.

## Capabilities

### New Capabilities

- `xlinks-provider-adapter`: Defines Xlinks authentication, endpoint configuration, `gpt-image-2` text-to-image execution, parameter normalization, result handling, failure semantics, provider selection, and future capability boundaries.

### Modified Capabilities

None. Existing server gateway, database model configuration, Cloud composition, and ticket-accounting requirements already define provider-neutral behavior that the Xlinks adapter must satisfy.

## Impact

- Provider/catalog infrastructure: `config/model_catalog/`, generated catalog artifacts, `src/utils/provider_registry.py`, `src/utils/endpoints.py`, and provider media/runtime routing.
- Runtime adapters: a new Xlinks adapter under `src/models/` and Cloud `RequestScopedModelClientFactory` construction.
- Configuration surfaces: `.env.example`, server-owned PC route activation, secret-reference allowlists, and deployment configuration where provider credentials are declared.
- Cloud character UI: safe effective-engine projection, read-only `GPT Image 2 · Xlinks` status, and capability-driven `角色设定图` template gating without browser model/provider controls.
- Tests: adapter request/response contracts, output decoding/downloading, retry safety, error redaction, provider registry selection, Cloud factory routing, safe route projection, PC character-workbench behavior, catalog validation, and character T2I integration.
- Documentation: repo-local Xlinks evidence capture sourced from `https://api.xlinks.site/api-docs` (accessed 2026-08-23), with an authenticated smoke-test checklist required before enabling the route in production.
- No database migration or breaking API change is expected. The existing `gpt-image-2` model ID and MuleRouter behavior remain valid.
