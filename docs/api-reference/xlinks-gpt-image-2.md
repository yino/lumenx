# Xlinks GPT Image 2 API Evidence

- Source: <https://api.xlinks.site/api-docs>
- Captured: 2026-08-23
- Scope: LumenX PC/Cloud `image.t2i` only

## Confirmed public contract

The public API documentation describes an OpenAI-compatible gateway whose base URL is the current site origin plus `/v1` (`https://api.xlinks.site/v1` for the supplied deployment). Every public API request uses `Authorization: Bearer <token>`. The portal identifies API tokens with the `sk-nexus-` prefix and documents `GET /v1/models`.

The public documentation does not document an image endpoint or its request/response body. Unauthenticated probes performed on 2026-08-23 returned the gateway's authentication error for both `POST /v1/images/generations` and `POST /v1/images/edits`, confirming that both paths are routed but not their payload contract.

## Workbench observations

The authenticated portal's public JavaScript bundle exposes `gpt-image-2` in text-to-image and image-to-image modes. Its creation form offers:

- Sizes: `auto`, `1024x1024`, `1536x1024`, `1024x1536`
- Quality: `auto`, `low`, `medium`, `high`
- Output format: `png`, `jpeg`, `webp`
- Background: `auto`, `opaque`, `transparent`
- Count: 1 through 4

The portal submits those values to its private `/api/creation-tasks` workflow with a portal API-key id. That private workflow is not used by LumenX and is not evidence that the same multipart contract is accepted by the public API.

## LumenX first-release contract

Until the credentialed smoke test proves otherwise, LumenX uses the conservative OpenAI-style request below and accepts exactly one of `data[0].b64_json` or `data[0].url`:

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

LumenX normalizes any portrait, landscape, or square request to one of the three explicit sizes. The adapter rejects reference images, non-PNG output, multiple outputs, malformed media, and non-HTTPS result URLs before the result enters object storage.

## Credentialed verification

Run `scripts/xlinks_t2i_smoke.py` with `XLINKS_API_KEY` present in the process environment. The utility performs `GET /v1/models` and one low-quality `gpt-image-2` request, saves the image only under a temporary directory, and prints redacted metadata. It never prints the token, base64 payload, or signed output URL.

The credentialed smoke test passed on 2026-08-23:

- `GET /v1/models`: HTTP 200, `application/json; charset=utf-8`, request ID present, 11 visible models, `gpt-image-2` present, 0.455 seconds.
- `POST /v1/images/generations`: HTTP 200, `application/json; charset=utf-8`, request ID present, 19.623 seconds.
- Response shape: exactly one `data` item containing `b64_json`; no `url` was present.
- Decoded output: valid PNG, 1,941,684 bytes. The generated file stayed in a temporary directory and was not committed.
- The successful response did not expose billing fields. Billing behavior for failures and ambiguous timeouts remains unresolved.

The smoke request used `quality=low`, `size=1024x1024`, `n=1`, `output_format=png`, and `background=auto`. This confirms the first-release request body and base64 response path. The URL response path remains covered by synthetic adapter tests because the successful provider response did not use it.
