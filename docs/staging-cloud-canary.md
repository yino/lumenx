# LumenX staging 云端 canary

本流程只在与生产数据隔离的 staging 执行，用于补充确定性 CI。它必须使用真实私有 OSS 和一个低成本供应商任务，但不得使用真实用户剧本、素材、手机号或生产密钥。

## 前置条件

1. 已通过 `.github/workflows/cloud-release-check.yml`，并取得对应修订的中文证据报告。
2. staging 使用 PostgreSQL 16、Redis、私有 OSS Bucket、独立供应商子账号和非超级应用角色。
3. 注册模式和新 AI 任务初始均关闭；staging 中只有平台管理员和专用 canary 用户。
4. 记录修订 SHA、迁移 head、激活配置版本和执行人，不记录 secret、邀请码、prompt 或原始供应商响应。

## 自动化门禁

真实 canary 使用 `scripts/staging_cloud_canary.py`，分为三个阶段。脚本不会创建或记录手机号、密码和邀请码；管理员及两个 canary 用户凭据只能通过以下运行时环境变量注入：

- `LUMENX_CANARY_ADMIN_PHONE` / `LUMENX_CANARY_ADMIN_PASSWORD`
- `LUMENX_CANARY_USER_PHONE` / `LUMENX_CANARY_USER_PASSWORD`
- `LUMENX_CANARY_SECONDARY_PHONE` / `LUMENX_CANARY_SECONDARY_PASSWORD`

所有命令必须在 staging 的 backend 容器环境中运行，以便使用同一只读对账边界。脚本会比较本地运行容器与目标 API 实时返回的四项资源指纹，防止跨环境读取错误数据库。环境名必须明确包含 `staging`、`stage`、`preprod` 或 `canary`，脚本拒绝生产环境名和非 HTTPS origin。

执行前还必须从 `docs/staging-isolation-manifest.example.json` 创建隔离清单。清单只写 PostgreSQL、Redis、OSS Bucket 和供应商账号的 `sha256:` 指纹，不写原始地址、Bucket 名或账号；staging/production 指纹必须不同，审核时间不能超过 30 天。

部署时为 backend 配置以下非密钥稳定标识：

- `LUMENX_DATABASE_RESOURCE_ID`：云数据库实例/集群，或本地部署的宿主机/卷运维库存标识；数据库名由连接配置自动纳入指纹，不能只写 Compose 服务别名 `postgres`。
- `LUMENX_REDIS_RESOURCE_ID`：云 Redis 实例，或本地部署的宿主机/卷运维库存标识；不能只写 Compose 服务别名 `redis` 或逻辑 DB 编号。
- `LUMENX_PROVIDER_ACCOUNT_ID`：供应商控制台中与当前凭据实际归属一致的账号或子账号标识。

这些值不能是环境别名或任意占位值。平台管理员分别从 staging 和 production 的 `/api/v1/admin/configuration/deployment-state` 获取 `resource_fingerprints`，经双人复核后写入清单。接口只返回哈希，不返回数据库/Redis 凭据、原始端点、Bucket 名或供应商账号标识。canary 的 `preflight`、`execute`、`finalize` 都会把清单中的 staging 指纹与目标部署实时返回值逐项比较；缺失或不一致时会在任何 OSS/供应商操作前失败。

### 1. 双关闭预检

保持数据库注册模式、数据库 AI 新任务开关和两个部署熔断全部关闭。把上述凭据预先放入当前 shell 或 secret 注入工具，只向 Compose 传变量名：

```bash
docker compose run --rm \
  -e LUMENX_CANARY_ADMIN_PHONE \
  -e LUMENX_CANARY_ADMIN_PASSWORD \
  backend python -m scripts.staging_cloud_canary preflight \
  --environment lumenx-staging \
  --origin https://staging.example.com \
  --revision <revision> \
  --isolation-manifest /secure-input/staging-isolation.json \
  --evidence-path output/canary/preflight.md
```

预检会验证 cloud 模式、真实供应商适配器、私有 OSS、readiness、实时资源指纹和双关闭状态，不调用供应商。

### 2. 一次最低成本调用

先记录一个已经校验的“双关闭”配置版本 ID。再创建临时配置：注册仍为 `disabled`，只开放一个已审核的最低成本能力/主路由，并允许 AI 新任务。保持注册部署熔断为 `true`，只把 AI 部署熔断临时设为 `false` 后滚动 backend/worker。

```bash
docker compose run --rm \
  -e LUMENX_CANARY_ADMIN_PHONE \
  -e LUMENX_CANARY_ADMIN_PASSWORD \
  -e LUMENX_CANARY_USER_PHONE \
  -e LUMENX_CANARY_USER_PASSWORD \
  -e LUMENX_CANARY_SECONDARY_PHONE \
  -e LUMENX_CANARY_SECONDARY_PASSWORD \
  backend python -m scripts.staging_cloud_canary execute \
  --environment lumenx-staging \
  --confirm-isolated-staging lumenx-staging \
  --confirm-paid-provider-call RUN_ONE_MINIMUM_COST_CANARY \
  --origin https://staging.example.com \
  --revision <revision> \
  --isolation-manifest /secure-input/staging-isolation.json \
  --closed-config-version-id <closed-config-id> \
  --expected-provider-model-id <reviewed-model-id> \
  --parameters-json '<reviewed-minimum-parameters-json>' \
  --max-quoted-microtickets <reviewed-upper-bound> \
  --state-path output/canary/state.json \
  --evidence-path output/canary/execute.md
```

脚本验证工作区/媒体跨用户拒绝、签名 TTL、审核模型、最大预扣、结果私有媒体、用量、结算和定向对账，并在成功或失败后都尝试回滚到指定双关闭配置。供应商 request/task ID 只以短哈希写入证据。

### 3. 恢复熔断并 finalize

execute 通过不代表可以开放。必须把 `LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED` 恢复为 `true`，确认注册熔断仍为 `true`，滚动 backend/worker 后执行：

```bash
docker compose run --rm \
  -e LUMENX_CANARY_ADMIN_PHONE \
  -e LUMENX_CANARY_ADMIN_PASSWORD \
  backend python -m scripts.staging_cloud_canary finalize \
  --environment lumenx-staging \
  --confirm-isolated-staging lumenx-staging \
  --origin https://staging.example.com \
  --revision <revision> \
  --isolation-manifest /secure-input/staging-isolation.json \
  --state-path output/canary/state.json \
  --evidence-path output/canary/final.md
```

只有 finalize 重新观察到数据库和部署双关闭、且最终对账问题数为 0，才会产生“仅限邀请制灰度”的 GO 结论。

## 私有 OSS canary

1. 管理员创建绑定 canary 手机号的一次性邀请，注册专用用户。
2. 上传仓库提供的无敏感 1x1 PNG，记录用户 ID、工作区 ID、媒体 ID 和 OSS object key 的哈希。
3. 确认 Bucket ACL 为 private，未签名公网请求返回拒绝。
4. 通过 `/api/v1/media/{id}/access` 获取短期签名地址，确认有效期不超过当前数据库策略；地址过期后必须拒绝。
5. 用第二个 canary 用户请求该媒体，确认 API 返回 404 且审计中没有泄露 object key。

## 低成本供应商 canary

1. 创建只开放一个低成本 `image.t2i` 路由的新配置版本，校验后激活；将 `tokens_per_ticket`、计量公式和每用户并发记录到证据报告。
2. 在隔离 staging 临时打开新 AI 任务，只让专用 canary 用户在线，提交一张最低规格图片。
3. 记录本地 task ID、attempt ID、供应商 task/request ID、配置版本、预扣金额、实际 token、结算算力券和结果 media ID；不记录 prompt 或原始 payload。
4. 确认任务按原快照完成，输出进入私有 OSS，预扣关闭，用量事件与不可变流水一一对应。
5. 运行 `python -m src.platform.ticket_reconciliation --admin-user-id <管理员ID>`，要求问题数为 0。
6. 立即重新关闭新 AI 任务；注册保持 `disabled`，直到正式灰度时才切换为 `invite_only`。

## 失败处理

任何 OSS、供应商、结算、RLS 或对账异常都判定 canary 失败。保持两个入口关闭，不盲目重提已被供应商受理的任务，不修改原流水。把任务转入人工复核，保存关联 ID 和结构化日志，按 `docs/cloud-operations.md` 的灰度回滚流程处理。

## 证据字段

最终中文报告至少补充：staging 环境名、修订 SHA、迁移 head、配置版本、OSS canary 媒体 ID、供应商 canary task/attempt/request ID、签名 TTL、计量 token、结算算力券、对账结果和 go/no-go 决策。所有凭据、邀请码、手机号、原始用户内容和签名 URL 必须脱敏或省略。
