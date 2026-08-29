# Short-drama Agent 发布说明

## 启用顺序

1. 执行 `alembic upgrade head`，创建 `agent_runs` 表。
2. 安装 `requirements.txt` 中锁定的 LangGraph 与 PostgreSQL checkpoint 依赖。
3. 在平台配置的 `feature_flags` 中设置 `short_drama_agent_enabled=true`，并将允许的 Profile 写入 `short_drama_agent_profiles`。默认灰度名单只有 `grok-imagine-video`。
4. 先设置 `short_drama_agent_shadow_mode=true` 验证编译与质检；预览运行不会创建视频任务。
5. 关闭 shadow 后，保留旧 `/projects/{id}/video_tasks` 入口作为回退，再逐步开放人工审批和提交。

## 数据与安全

Agent Run 的 `payload` 是经过敏感字段过滤的状态快照，只保存应用媒体 ID 和 reference map，不保存签名 URL、文件路径、API key 或 Provider 凭据。所有视频任务必须通过 Cloud AI Gateway 和现有计费/路由服务创建。

## 回滚

将 `short_drama_agent_enabled` 设为 `false`，客户端自动继续使用旧入口；已提交的 Provider 任务不删除，按现有 AI task 恢复策略处理。仅在确认没有运行中的 Agent worker 后执行数据库降级。

## 诊断

通过 Agent Run 状态接口查看当前阶段、阶段事件、质检 finding、审批记录和 task IDs。`stage_events` 可用于定位 worker 中断点；同一 run/shot 的幂等键可安全重试，不会重复创建计费任务。
