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

LumenX maps `video.t2v` to a request without `image` and `video.i2v` to one
HTTPS image URL in `image`. The adapter converts `resolution` values such as
`720p` and `1080p` to `width` and `height`, stores the provider task ID before
polling, downloads only HTTPS MP4 output, and never retries an ambiguous task
creation request.

The NewAPI documentation defines the transport contract, but does not promise
which upstream models a deployment enables. Xlinks must expose the
`grok-imagine-video` model before this route can generate media.
