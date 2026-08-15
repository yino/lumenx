# LumenX 云端发布验收记录

## 验收范围

- OpenSpec 变更：`add-user-workspace-ticket-platform`
- 验收日期：2026-08-14
- 数据库：一次性 PostgreSQL 16 Alpine，未挂载持久卷
- 应用角色：`lumenx_app`，已确认 `NOSUPERUSER NOBYPASSRLS`
- 功能开关：用户注册与 AI 新任务保持默认关闭

## PostgreSQL 分阶段迁移

1. 从空库升级到兼容基线 `0004_admin_configuration`，实际版本号长度为 24。
2. 从兼容基线升级到 `0008_audit_event_insert_policy (head)`。
3. 核对 30 条 RLS policy 已创建。
4. 核对以下不可变触发器已创建：
   - `ticket_ledger_append_only`
   - `model_configs_immutable`
   - `platform_configs_immutable`
   - `config_versions_content_immutable`
5. 应用数据库角色初始化脚本连续执行两次成功，验证已有库升级幂等。

验收中发现并修复：

- `0004_administration_configuration` 超过 Alembic 默认 32 字符版本列，真实迁移失败。修订号已缩短，并增加迁移链长度、唯一性和单 head 回归测试。
- 首次注册事务缺少明确外键插入顺序，PostgreSQL 会先写会话导致失败。现已在同一事务内按用户、钱包、依赖记录三阶段 flush，失败仍整体回滚。
- Compose 原先让应用复用 PostgreSQL 超级用户，导致 RLS 被绕过。现已拆分迁移/备份管理员与非超级应用角色，并让 `/ready` 拒绝 `SUPERUSER` 或 `BYPASSRLS` 连接。

## 数据隔离与注册

- 使用 `lumenx_app` 完成首个平台管理员注册事务。
- 用户、默认工作区、钱包、初始流水和会话各写入 1 条。
- 创建第二个普通用户后，两个用户上下文各只能读取自己的用户、工作区和钱包。
- 创建一次性项目后，所有者可读取，另一用户读取结果为空。

## 本地导入 dry-run

对仓库现有 `output/projects.json` 执行 dry-run，缺失媒体策略为清空引用：

- 计划项目：2
- 计划资产：9
- 计划媒体：28
- 媒体总量：58,872,006 字节
- 阻断问题：0
- dry-run 后项目、资产和媒体业务表仍为 0；仅保存 1 个预检批次及 39 个预检条目。

## 算力券与任务对账

- 钱包：2
- 流水：2
- 预扣：0
- AI 任务：0
- 用量事件：0
- 对账问题：0
- 结果：一致

## 自动化回归

- 后端全量：526 passed
- worker、恢复、派发、维护、对账、导入和桌面专项：52 passed
- 前端逻辑测试：167 passed
- 前端 UI 测试：61 passed
- 前端类型检查：通过
- 前端生产构建：通过
- 云端负载模型：10,000 用户、10,000 日 AI 任务、单用户并发 2、全局 worker 并发 8，通过
- `docker compose config --quiet`：通过
- Shell 语法检查：通过
- `git diff --check`：通过

Next.js 构建仍提示静态导出不应用 rewrites；该警告不影响本次静态产物生成，但部署时必须由反向代理提供同源 API 路由。

## 云端强化复验（2026-08-15）

- OpenSpec 变更：`harden-cloud-launch-readiness`
- 后端全量：562 passed
- 前端逻辑测试：168 passed
- 前端 UI 测试：61 passed
- 前端类型检查：通过
- 前端 Lint：0 error；保留既有 warning
- `docker compose config --quiet`：通过
- OpenSpec strict validation：通过
- `git diff --check`：通过
- 真实 staging 私有 OSS/供应商 canary：未执行，等待隔离资源、凭据和一次付费调用的人工批准
- 生产旧 API 兼容观察窗口：未开始，兼容路由保持启用

本次复验新增在线资源指纹门禁。真实 canary 的三个阶段都必须确认隔离清单与当前部署的 PostgreSQL、Redis、OSS Bucket 和供应商账号指纹一致，避免清单被误用于另一套环境。

## 发布结论

迁移、注册、RLS、导入预检、账务对账、后端、前端、worker 和桌面回归均通过。上线仍应遵循灰度顺序：保持注册和 AI 新任务关闭，先执行数据库角色 bootstrap 与 migration，核对 `/ready`，再开放已有管理员的 AI 新任务，最后开放用户注册。
