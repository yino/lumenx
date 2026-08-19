# LumenX 云端运维手册

本文档适用于 `cloud` 部署。桌面版仍使用本地适配器，不需要 PostgreSQL、Redis、OSS 或算力券结算。

仓库中的 `docker-compose.override.yml` 仅用于本地 Docker。生产操作前必须固定基础文件，避免加载本地环境变量和命名卷覆盖：

```bash
export COMPOSE_FILE=docker-compose.yml
```

## 上线前准备

1. 准备 PostgreSQL 16、Redis 7.4、私有 OSS Bucket 和 HTTPS 域名。
2. 从 `secrets/*.txt.example` 创建真实 secret 文件，权限限制为部署账号可读。`postgres_password.txt` 与 `postgres_app_password.txt` 必须使用不同随机密码。
3. 配置 `LUMENX_ALLOWED_ORIGINS` 为真实前端源，不能使用通配符。
4. 配置 `LUMENX_PROVIDER_SECRET_REFS`。数据库只保存引用名，不保存供应商明文密钥。
5. 通过 Compose 或环境变量配置 `LUMENX_GLOBAL_WORKER_CONCURRENCY`，初始建议为 `8`。该值属于部署容量，只在管理页只读展示，不能通过数据库配置激活。
6. 保持 `LUMENX_REGISTRATION_EMERGENCY_DISABLED=true` 和 `LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED=true`。默认 Compose 即为关闭入口；只有中文发布证据、staging canary 和对账全部通过后，才显式改为 `false` 并滚动发布。

检查 Compose 展开结果，不输出 secret 内容：

```bash
docker compose config --quiet
docker compose up -d postgres redis
docker compose run --rm database-role-bootstrap
docker compose run --rm migration
docker compose up -d backend ai-worker maintenance-worker scheduler postgres-backup frontend
docker compose ps
```

后端 `/ready`、PostgreSQL、Redis 和 worker 健康检查必须全部通过。注册开放前先完成本文档的迁移、种子、管理员和对账步骤。

云端浏览器只使用同源 `/api/v1`。Nginx 是唯一公开入口，默认 Compose 不发布 backend 的 `17177` 端口；未知 `/api/v1/*` 必须返回后端 JSON 404，不能回退到 `index.html`。容器内 Uvicorn 可以信任 Nginx 转发地址，因为该端口不公开；非 Compose 部署必须把 `--forwarded-allow-ips` 收窄为实际代理 CIDR，禁止在公开 backend 上信任任意转发头。

backend、AI worker 和维护任务只能使用 `lumenx_app`（或 `LUMENX_DATABASE_USER` 指定的等价角色）。该角色必须是 `NOSUPERUSER NOBYPASSRLS`；迁移和备份才使用 `POSTGRES_USER`。`/ready` 会检查运行角色，误用 PostgreSQL 管理员时拒绝就绪。`database-role-bootstrap` 是幂等的，已有数据卷升级时也必须先执行，用于补齐现有对象权限和未来默认权限。

## 数据库迁移

迁移使用 expand/contract 原则：先增加兼容结构并部署兼容代码，观察稳定后才能删除旧结构。禁止在上线事务中执行破坏性降级。

```bash
docker compose run --rm database-role-bootstrap
docker compose run --rm migration alembic current
docker compose run --rm migration alembic upgrade head
docker compose run --rm migration alembic current
```

先在 staging 对生产备份副本执行同一迁移。出现失败时停止新版本部署，保留数据库现场和迁移日志，不运行 `alembic downgrade` 删除账务或审计记录。

`0014_expand_admin_console` 会新增人工充值订单/事件/对账报告和系统媒体 scope，并把既有媒体回填为 `scope=user`；`0015_system_media_copy_access` 保留用户系统场景副本对源媒体的只读访问；`0017_physical_admin_identity` 新增独立 `admin_users/admin_sessions`，撤销旧共享管理员会话并删除 `users.is_platform_admin`；`0018_admin_recovery_rls` 为显式唯一管理员身份恢复提供受限的系统更新策略。原用户、钱包、工作区和资产保持普通用户归属，不会转换或关联到管理员。全新库和从 `0008_audit_event_insert_policy` 升级的旧库都必须运行 `scripts/verify_postgres_migrations.py`。产生独立管理员、管理员审计或财务记录后禁止降级合并身份或删除结构；后续错误只能用新的前向迁移和补偿流水修正。

## 首个平台管理员

Compose 会在迁移成功后通过 `admin-bootstrap` 幂等创建首个平台管理员。本地默认用户名是 `admin`；本地初始密码由 `.env` 的 `LUMENX_BOOTSTRAP_ADMIN_PASSWORD` 提供。生产部署必须在首次启动前替换该密码，禁止沿用仓库示例值。

```bash
docker compose run --rm admin-bootstrap
docker compose run --rm backend python -m src.platform.bootstrap_admin --username <管理员用户名>
```

命令从 `LUMENX_BOOTSTRAP_ADMIN_PASSWORD` 读取密码，数据库只保存 Argon2 哈希。命令不会把密码输出到日志；重复执行不会重置既有管理员密码，也不会创建普通用户、钱包或工作区。记录输出的管理员 ID，后续模型种子和对账命令需要它。管理员遗失密码时，替换环境中的新密码后显式执行 `python -m src.platform.bootstrap_admin --username <管理员用户名> --rotate-existing`；该操作撤销全部后台会话并要求下次登录改密。完整后台操作和事故处置见 `docs/system-admin-console.md`。

本地 Compose 保留数据卷中若恰好有一个不同用户名的独立管理员，`docker-compose.override.yml` 会启用一次性自动接管：保留管理员 ID 和全部审计/财务关联，将其改为 `.env` 配置身份，撤销旧后台会话并追加审计。配置身份已存在时不会再次轮换密码；零个或多个异名管理员时拒绝自动接管。生产和发布配置不启用该开关，普通 bootstrap 继续失败关闭；确认目标后使用 `make docker-admin-adopt` 显式恢复。

## 模型和平台配置

仓库模型目录只用于生成待审核草稿，运行时权威配置在 PostgreSQL。首次种子命令不会覆盖管理员已创建的版本：

```bash
docker compose run --rm backend python -m src.platform.model_catalog_seeder \
  --admin-id <管理员ID> \
  --tokens-per-ticket 1000 \
  --reason "首次导入仓库模型目录"
```

在平台管理页检查模型能力、主/备用路由、参数边界、计量公式、凭据引用和每用户并发。全局 worker 并发只读来自部署环境。先执行“校验”，再填写原因激活。回滚配置也会创建新版本，不能修改历史版本。

生产首次激活时使用注册模式 `disabled`，并保持“允许创建 AI 新任务”关闭。完成中文发布证据、迁移、部署栈 smoke、真实 staging canary 和数据对账后，先显式解除相应环境熔断，再按运营决策切换为 `invite_only` 或 `open`。`open` 表示已明确接受暂不校验验证码的手机号注册风险；`verified_open` 预留给未来短信验证码，验证码服务未就绪时激活会失败。本地 Compose 通过独立的 `registration-mode-bootstrap` 默认激活 `open`，生产默认仍为 `disabled`。

邀请只能由平台管理员创建，必须绑定规范化手机号、填写中文原因和过期时间。邀请码明文只在创建响应中显示一次，数据库只保存 HMAC 摘要；不要把邀请码写入日志、工单或长期文档。邀请可在消费前撤销，注册会把邀请消费与用户、钱包、初始流水、默认工作区和会话放在同一事务中。环境变量 `LUMENX_REGISTRATION_EMERGENCY_DISABLED` 和 `LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED` 是只关不启的紧急熔断，不能绕过数据库策略。

数据库激活配置是业务策略权威，包括算力券汇率、注册模式和赠送、会话策略、每用户 AI 并发、媒体签名 TTL、保留期及陈旧预扣阈值。新请求每次确认 active version，再按版本 ID 复用不可变 payload；AI 任务、会话、邀请决策和账务流水保留创建时快照，不随以后激活而改写。数据库/Redis/OSS 地址、密钥、可信代理和 worker 容量始终由部署环境管理。

## 升级与兼容窗口

升级前先设置两个紧急熔断为 `true`，执行数据库角色 bootstrap、完整迁移链和旧配置升级夹具。旧 `registration_enabled=false` 会迁移为 `disabled`，`true` 会迁移为 `invite_only`，不会产生未验证开放注册。升级不会删除邀请、会话快照、AI 任务、用量、预扣、流水或审计历史。

发布 `/api/v1` 时先部署支持新命名空间的 Nginx，再部署静态前端。`LUMENX_LEGACY_API_COMPAT=true` 只用于有界兼容窗口；HTML 必须 `no-store`。观察兼容路由计数和访问日志，确认所有受支持客户端都不再调用无版本云端根路径后，才把该变量切为 `false`。桌面本地根路由不受此开关影响。

兼容路由退出使用 `scripts/legacy_api_compat_evidence.py` 生成证据，不能只凭一次指标查询或开发者判断关闭。门禁要求：

1. 默认至少 168 小时的完整观察窗口，收集全部 Nginx `lumenx-legacy-api.log` 轮转文件。专用日志位于 frontend 容器 `/var/log/lumenx/`，由 `lumenx-nginx-legacy-logs` 命名卷持久化，容器替换时不得删除该卷；生产日志系统应从此目录采集并按观察窗口保留。
2. 窗口起止各保存一次 `/api/v1/admin/observability/metrics` 管理员指标快照；计数器不得在窗口中重置，增量必须为 0。
3. 提供受支持云端客户端清单；每个客户端必须有责任人、在窗口内完成验证，并声明使用 `/api/v1`。
4. 操作员用 `环境|UTC开始|UTC结束` 精确确认日志覆盖完整窗口。证据只保存文件哈希和汇总数，不保存 IP、路径或 User-Agent。

客户端清单可从 `docs/supported-cloud-clients.example.json` 创建。示例执行：

```bash
python -m scripts.legacy_api_compat_evidence \
  --environment production-cn \
  --window-start 2026-08-01T00:00:00Z \
  --window-end 2026-08-08T00:00:00Z \
  --access-log /secure-export/lumenx-legacy-api.log.1 \
  --access-log /secure-export/lumenx-legacy-api.log \
  --metrics-snapshot /secure-export/legacy-metrics-start.json \
  --metrics-snapshot /secure-export/legacy-metrics-end.json \
  --supported-clients /secure-export/supported-cloud-clients.json \
  --confirm-complete-log-window 'production-cn|2026-08-01T00:00:00+00:00|2026-08-08T00:00:00+00:00' \
  --evidence-path /secure-export/legacy-compat-evidence.md
```

工具返回 GO 后也不会自动切换部署变量。将 `LUMENX_LEGACY_API_COMPAT=false` 作为单独受控发布，继续观察版本化 API 的 404、前端错误率和支持反馈；异常时只恢复兼容开关，不回退数据。

回滚只回退应用和静态产物，不执行会丢失历史的 Alembic downgrade。保持注册和新 AI 工作关闭，继续运行 worker 处理已被供应商受理的任务，完成任务/预扣和钱包/流水对账后再切流量。错误配置通过创建并激活新版本修正，不直接编辑历史配置行。

## 算力券人工操作

平台管理页提供赠送、扣减和补偿。每次操作必须填写具体原因，系统写入不可变流水和审计事件。显示单位为算力券，内部精度为一百万分之一张。

- 赠送：运营发放或初始补贴。
- 扣减：仅在余额充足且依据明确时使用。
- 补偿：纠正故障影响，不修改原结算流水。

禁止直接更新 `ticket_wallets` 或删除 `ticket_ledger`。误操作必须追加反向调整，并在原因中引用工单、任务 ID 和原流水 ID。

## 备份与恢复

`postgres-backup` 每日生成 custom-format dump、SHA-256 文件和目录清单，并保留 14 天。`scheduler` 每日验证最新备份时效、校验和，以及审计/配置关键表是否在清单内。

每月至少执行一次隔离恢复演练：

```bash
createdb lumenx_restore_test
pg_restore --exit-on-error --clean --if-exists --no-owner \
  --dbname=lumenx_restore_test /backups/lumenx-<时间>.dump
```

恢复后检查迁移版本、用户/工作区数量、`audit_events`、`config_versions`、`ticket_wallets`、`ticket_ledger`、`ticket_holds`、`usage_events` 和 `ai_tasks`。再运行算力券对账。OSS 需要独立启用版本控制或生命周期备份；数据库恢复不会自动恢复已删除对象。

## 对账与日常维护

定时维护运行在独立 `maintenance` 队列：过期会话每 15 分钟、陈旧任务/预扣每 10 分钟、孤儿媒体每小时、保留期清理和备份验证每日执行。

手工只读对账：

```bash
docker compose run --rm backend python -m src.platform.ticket_reconciliation \
  --admin-id <管理员ID> \
  --stale-after-minutes 30
```

退出码 `0` 表示一致，`2` 表示发现问题。对账不会重写历史。遇到 `WALLET_LEDGER_CACHE_MISMATCH`、`TERMINAL_TASK_OPEN_HOLD`、`USAGE_LEDGER_CORRELATION_MISSING` 时先阻断新 AI 任务，按任务和流水关联调查，再使用补偿流水处理，不直接改旧记录。

负载门槛可在部署前复跑：

```bash
python scripts/load_test_cloud.py
```

默认验证 10,000 注册用户、10,000 日任务、单用户并发 2 和全局 worker 背压。

## 凭据轮换

1. 在 secret 管理系统创建新值，保留旧值供在途任务短期使用。
2. 若引用名变化，创建并校验新的配置版本；不要编辑已激活版本。
3. 更新 Docker secret，滚动重启 backend、ai-worker 和 maintenance-worker。
4. 提交新任务做健康验证，确认旧任务仍按其快照完成。
5. 队列清空且供应商确认无旧凭据请求后撤销旧值。

会话密钥轮换会使现有会话失效，应提前公告并安排登录低峰。数据库、OSS 和供应商密钥不能写入日志、审计摘要、任务 payload 或配置 JSON。

## 故障处置

### Redis 或队列不可用

停止新 AI 请求入口，保留 PostgreSQL 中的 `queued` 任务和预扣。恢复 Redis 后用相同任务 ID 重新派发；禁止创建新任务替代旧任务。检查队列深度和派发失败指标。

### worker 在供应商受理后崩溃

查看任务尝试中的供应商请求/任务 ID。已知供应商任务 ID 时只恢复轮询；标记为 `ambiguous` 时进入人工调查，禁止盲目重提。供应商已计费而本地处理失败时结算实际用量并进入人工复核。

### OSS 故障

暂停需要上传或签名的操作。供应商已计费的输出保存失败不能全额退款，应进入人工复核；未计费失败释放全部预扣。恢复后运行孤儿媒体清理和任务对账。

### 账务不一致

立即关闭新 AI 工作，保留数据库和日志快照，运行只读对账。不得删除或修改原流水；修正使用带原因的补偿/扣减流水。确认所有预扣关闭后再恢复入口。

### 安全事件

撤销受影响用户会话，轮换相关凭据，检查关联 ID、审计事件和结构化日志。不要把用户剧本、prompt、供应商原始 payload 或 secret 粘贴到工单。若涉及数据越权，保持数据库和对象存储访问日志并停止相关接口。

## 灰度回滚

需要回退 API 或发生供应商、队列、账务故障时，按以下顺序执行：

1. 立即设置 `LUMENX_NEW_AI_TASKS_EMERGENCY_DISABLED=true` 并滚动重启 backend，确认新 AI 请求返回 `AI_NEW_TASKS_DISABLED`。已有任务查询、取消和管理接口必须仍可使用。
2. 必要时设置 `LUMENX_REGISTRATION_EMERGENCY_DISABLED=true`，阻止新增用户，但不撤销现有用户会话。
3. 保持 ai-worker 和 maintenance-worker 运行。排空尚未提交供应商的队列；已知供应商任务 ID 的任务继续轮询，受理状态不明确的任务转人工复核，禁止盲目重提。
4. 运行只读算力券/任务对账，逐一处理终态任务的未关闭预扣。未计费任务释放预扣；供应商已计费任务按快照结算。不得更新或删除历史 `ticket_ledger`，纠错只能追加补偿流水。
5. 将流量切回上一兼容 API 版本。数据库只执行 expand/contract 兼容迁移，不执行会删除用户、任务、审计、配置或账务数据的 schema downgrade。
6. 修复后创建新的平台配置版本并填写原因，先关闭两个功能开关激活；完成迁移、导入 dry-run、对账和回归验证后，先恢复 AI 新任务，最后恢复注册。确认稳定后再移除环境紧急熔断。

第 6 步中的“恢复注册”应恢复到事故前已审核的 `invite_only` 或 `open` 模式，不能在事故处理中临时扩大开放范围。`open` 不校验验证码，启用前必须明确记录风险接受；如需停止新注册，使用 `disabled` 或紧急熔断。回滚期间不得撤销已消费邀请或删除未消费邀请，并保留邀请与配置审计历史。

回滚期间保留 PostgreSQL、OSS、Redis 派发日志、关联 ID 和配置版本。Redis 队列可重建，PostgreSQL 中的任务、预扣、用量和不可变流水才是恢复依据。
