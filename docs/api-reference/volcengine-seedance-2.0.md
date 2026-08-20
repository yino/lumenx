# Volcengine Ark Seedance 2.0 Video API

> Capture date: 2026-08-20
> Provider: Volcengine Ark (China, cn-beijing)
> Scope: Doubao Seedance 2.0, Agent Plan and standard pay-as-you-go API

## Sources

- Agent Plan quick start: https://console.volcengine.com/ark/region:cn-beijing/docs/82379/2373738?lang=zh
- Agent Plan visual model access: https://console.volcengine.com/ark/region:cn-beijing/docs/82379/2375486?lang=zh
- Create video task: https://docs.volcengine.com/docs/82379/1520757
- Query video task: https://www.volcengine.com/docs/82379/1521309
- Seedance 2.0 tutorial: https://www.volcengine.com/docs/82379/2291680
- General video generation tutorial: https://www.volcengine.com/docs/82379/2298881
- Trusted human materials: https://www.volcengine.com/docs/82379/2315856?lang=zh
- Advanced creator package and Assets API entitlement: https://www.volcengine.com/docs/82379/2377608?lang=zh

## Authentication and endpoints

Requests use `Authorization: Bearer <API_KEY>` and JSON request bodies.

Agent Plan uses a dedicated API key and a dedicated base path:

```text
POST https://ark.cn-beijing.volces.com/api/plan/v3/contents/generations/tasks
GET  https://ark.cn-beijing.volces.com/api/plan/v3/contents/generations/tasks/{id}
```

The standard pay-as-you-go Ark endpoint uses:

```text
POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
GET  https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks/{id}
```

Agent Plan keys and standard Ark keys must not be mixed. Agent Plan Large or a
higher package is required for video generation quota.

如果创建任务返回 `404 UnsupportedModel: The requested model does not support the
agent plan feature`，请求格式和图片通常已经通过，问题在账号侧：当前 Agent Plan
项目没有开通该视频模型，或填写的模型名不是该项目实际可用的模型/接入点 ID。请在
方舟控制台开通并选择视频模型后，把控制台显示的真实标识填入 `ARK_SEEDANCE_MODEL`。
普通按量方舟需要使用普通 API Key、`https://ark.cn-beijing.volces.com/api/v3`，以及
普通视频模型标识 `doubao-seedance-2-0-260128`，不能与 Agent Plan Key 混用。

## Model identifiers

| Access mode | Model identifier |
|---|---|
| Agent Plan | `doubao-seedance-2.0` |
| Standard Ark | `doubao-seedance-2-0-260128` |

## Request structure

Text-to-video sends one text item in `content`.

Image-to-video adds one `image_url` item. The Agent Plan example does not set a
role for a single first-frame image.

Reference-to-video adds up to nine images with `role: reference_image`:

```json
{
  "model": "doubao-seedance-2.0",
  "content": [
    {"type": "text", "text": "镜头缓慢推进"},
    {
      "type": "image_url",
      "image_url": {"url": "https://example.com/reference.png"},
      "role": "reference_image"
    }
  ],
  "generate_audio": false,
  "ratio": "16:9",
  "duration": 5,
  "resolution": "720p",
  "watermark": false
}
```

Images can be supplied through a public URL or Base64 data URL. Seedance 2.0
reference generation accepts at most nine images. A first/last-frame request
uses explicit `first_frame` and `last_frame` roles.

### Trusted human and virtual-person materials

A normal HTTPS URL carries image bytes but no Ark trusted-material identity.
For a real-person-like or certified virtual-person reference, first complete
the applicable authorization/certification flow in Ark's trusted-material
library, then submit the returned Asset ID as an `asset://` URI:

```json
{
  "type": "image_url",
  "image_url": {"url": "asset://asset-202602..."},
  "role": "reference_image"
}
```

LumenX stores this provider Asset ID alongside the image variant rather than
replacing its normal URL: the normal URL remains available for local preview,
while the Ark Seedance adapter prefers the bound `asset://` URI at generation
time. A binding does not itself certify the image; Ark still verifies that the
Asset ID exists, belongs to the calling account, and has the required status.

Programmatic ingestion through the Assets API is entitlement-gated. The
official advanced creator package table lists API-based real/virtual-person
material ingestion under advanced rights rather than base creation rights.
Do not invent an upload endpoint or silently treat an OSS/HTTPS upload as a
trusted material when the account lacks that entitlement.

## Async result

Task creation returns:

```json
{"id": "cgt-..."}
```

Query the task until `status` is `succeeded`, `failed`, or `expired`. Known
in-progress statuses include `queued` and `running`. A successful response puts
the expiring MP4 download URL at `content.video_url`.

```json
{
  "id": "cgt-...",
  "status": "succeeded",
  "content": {"video_url": "https://.../video.mp4"},
  "resolution": "720p",
  "ratio": "16:9",
  "duration": 5
}
```

Generated video URLs are retained for 24 hours and have a documented download
limit, so LumenX downloads the file into project output immediately after task
completion.
