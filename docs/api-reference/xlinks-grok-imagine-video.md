# Xlinks Grok Imagine Video API Evidence

- Source: <https://www.newapi.pro/zh/docs/api/ai-model/videos/createvideogeneration>
- Status source: <https://www.newapi.pro/zh/docs/api/ai-model/videos/getvideogeneration>
- Captured: 2026-08-25
- Scope: LumenX PC/Cloud `video.t2v` and `video.i2v`

Xlinks uses the open-source NewAPI video contract. The gateway is OpenAI-style
Bearer authenticated and exposes asynchronous video generation endpoints below
the configured `/v1` base URL:

```text
POST /v1/video/generations
GET  /v1/video/generations/{task_id}
```

The create request is JSON. The fields documented by NewAPI are `model`,
`prompt`, `image`, `duration`, `width`, `height`, `fps`, `seed`, `n`,
`response_format`, `user`, and `metadata`. The create response contains a
`task_id` and an initial `status` (`queued`). Status responses use `queued`,
`in_progress`, `completed`, or `failed`; a completed response returns an MP4
`url` and optional `metadata`.

LumenX maps `video.t2v` to a request without an image. For `video.i2v`, the
Xlinks deployment currently requires the upstream alias `image_url` (rather
than the generic NewAPI `image` field); the reference may be an HTTPS URL or a
base64 PNG, JPEG, or WebP data URI. The adapter converts `resolution` values
such as `720p` and `1080p` to `width` and `height`, stores the provider task ID
before polling, and downloads only HTTPS MP4 output.

If an Xlinks deployment returns the pre-task `fail_to_fetch_task` error for an
HTTPS image URL, the adapter downloads that image itself within a bounded
HTTPS request and retries once with an `image_url` data URI. Network timeouts
and all ambiguous task-creation failures are never retried.

The NewAPI documentation defines the transport contract, but does not promise
which upstream models a deployment enables. Xlinks must expose the
`grok-imagine-video` model before this route can generate media.

Xlinks currently wraps status responses in a business envelope such as
`{"code":"success","data":{"status":"SUCCESS","result_url":"..."}}`.
The adapter normalizes `QUEUED`, `not_start`, and `SUCCESS` to the NewAPI
`queued`, `in_progress`, and `completed` states. When both fields are present,
`result_url` is authoritative; the legacy nested `data.video.url` can point to
an expired token. Xlinks result URLs require the Bearer credential, while a
redirected external CDN URL must not receive that credential.
