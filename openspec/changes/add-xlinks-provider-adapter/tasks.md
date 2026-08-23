## 1. Documentation Evidence and Contract Gate

- [x] 1.1 Capture `https://api.xlinks.site/api-docs` into `docs/api-reference/xlinks-gpt-image-2.md` with source URL, 2026-08-23 capture date, confirmed authentication/base URL facts, endpoint probe results, workbench-observed parameters, inferred facts, and unresolved questions.
- [x] 1.2 Add a credentialed Xlinks T2I smoke-test utility that reads `XLINKS_API_KEY` only from the environment, writes output to a temporary directory, redacts credentials and image payloads, and records request/response metadata needed to validate the contract.
- [x] 1.3 Verify `gpt-image-2` with an authorized credential through `GET /v1/models` and one minimal `POST /v1/images/generations` call; record the redacted response shape, content type, request ID, latency, and available billing observations.

## 2. Xlinks Image Adapter

- [x] 2.1 Add `src/models/xlinks.py` with isolated Xlinks authentication, `XLINKS_BASE_URL` resolution, conservative request timeouts, safe error parsing/redaction, and a reusable provider HTTP boundary that does not depend on portal login APIs.
- [x] 2.2 Implement `XlinksImageModel` T2I request mapping for `gpt-image-2`, including non-empty prompts, orientation-preserving size normalization, allowed quality values, fixed `n=1`, fixed PNG output, default background handling, and omission/rejection of unsupported parameters.
- [x] 2.3 Implement bounded `b64_json` decoding and HTTPS URL download paths with content-type/signature validation, temporary-file writes, atomic output replacement, redirect/size limits, and rejection of malformed or multiple usable outputs.
- [x] 2.4 Capture `x-oneapi-request-id` through the existing provider-submission callback and return bounded provider usage metadata without logging request bodies, credentials, base64 output, or signed result URLs.
- [x] 2.5 Implement billing-safe failure classification so validation/authentication failures are nonretryable and ambiguous post-transmission timeouts are never blindly resubmitted.
- [x] 2.6 Add adapter unit tests for request headers/body, all size orientations, URL and base64 success, missing/extra outputs, invalid media, secret redaction, request-ID capture, explicit provider errors, and ambiguous timeout behavior.

## 3. Catalog and Capability-Aware Routing

- [x] 3.1 Register `xlinks` in provider backend validation and endpoint defaults, including `XLINKS_API_KEY` and default `XLINKS_BASE_URL=https://api.xlinks.site/v1`.
- [x] 3.2 Extend provider runtime metadata and resolution to select Xlinks only for `image.t2i` and reject Xlinks for `image.i2i` and video capabilities.
- [x] 3.3 Update `config/model_catalog/families/gpt-image.yaml` to add Xlinks backend/runtime/credential metadata while preserving the existing `gpt-image-2` identity and leaving non-PC catalog defaults unchanged.
- [x] 3.4 Constrain the first Xlinks T2I route to one PNG output and supported size/quality/background values so catalog and server parameter validation cannot request unsupported or silently discarded output.
- [x] 3.5 Regenerate backend/frontend catalog artifacts and schema with `python scripts/build_model_catalog.py`, then validate provider selection and capability rejection.

## 4. PC/Cloud Web Integration

- [x] 4.1 Register an `xlinks` builder in `RequestScopedModelClientFactory` that constructs a fresh `XlinksImageModel` only for `image.t2i` and rejects unsupported capabilities.
- [x] 4.2 Extend server secret-reference validation, deployment/provider allowlists, model catalog seeding, and readiness checks to accept `XLINKS_API_KEY` without exposing its value to the browser or audit projections.
- [x] 4.3 Add and activate the PC deployment's Xlinks `image.t2i` database route with server-owned parameter rules, `count=1`, PNG output, bounded metering, and no client provider override.
- [x] 4.4 Add a workspace-scoped safe effective-engine projection that returns only logical model/display identity, provider display identity, and UI-relevant capability flags while excluding credentials, secret references, base URLs, internal route IDs, pricing, and fallback policy.
- [x] 4.5 Update `CastWorkbenchModal` in PC/Cloud mode to render the effective engine as a read-only label, use the server-reported design-sheet capability, keep `角色设定图` locked when metadata is unavailable/ineligible, and expose no model/provider selector.
- [x] 4.6 Keep PC asset-generation payloads provider-neutral: the browser sends no model/provider/credential/route override and the server authoritatively resolves and revalidates the active `image.t2i` route at submission.
- [x] 4.7 Ensure Xlinks request IDs, attempt state, billable acknowledgement, raw usage, and ambiguous-timeout status flow through persistent task execution and ticket settlement without unsafe fallback or duplicate submission.
- [x] 4.8 Add Cloud factory, route-validation, worker execution, credential-boundary, safe-projection, provider-neutral request, provider metadata, failure/fallback, and output-storage tests for the Xlinks route.
- [x] 4.9 Keep `image.i2i`, video generation, the desktop application, desktop settings, and desktop packaging outside this change.

## 5. Verification and Release Readiness

- [x] 5.1 Run targeted Xlinks adapter, provider registry, catalog, Cloud routing, gateway, and deployment tests and resolve all failures.
- [x] 5.2 Run `python scripts/validate_model_catalog.py` and confirm generated backend/frontend artifacts match, defaults remain valid, and visible models retain documentation linkage.
- [ ] 5.3 Restore the full backend suite to green. The current run completed with 734 passing and five pre-existing failures in credential-secret assertions, safe text-result fixture setup, and a legacy video mock signature; all 94 Xlinks/catalog/routing/deployment tests pass.
- [x] 5.4 Run the targeted frontend deployment tests and the PC/Cloud production build; verify the character workbench shows a read-only engine label and no route selector.
- [x] 5.5 Confirm the credentialed smoke-test gate passed, then activate the PC deployment's server-owned `image.t2i` route as `gpt-image-2` on Xlinks.
- [x] 5.6 Verify the activated PC route from persisted configuration, the provider contract with a real redacted Xlinks generation, and the character workbench with a browser-level check showing `GPT Image 2 · Xlinks`, no selector, and enabled design-sheet support; do not create billable user project data solely for verification.
- [x] 5.7 Document rollback by returning the active PC `image.t2i` route to the previous provider without project data migration.
