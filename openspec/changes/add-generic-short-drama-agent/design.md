## Context

当前项目的短剧流程由 FastAPI、React 分镜工作台、Redis/Celery 后台任务、PostgreSQL 云端任务状态、模型目录和 Provider Adapter 共同承担。`seedance-short-drama` 已经提供了较完整的提示词生产规范，但它是外部 Skill 文档，不是应用内可恢复的执行流程；同时，素材标签、模型分支和音频/参数差异分散在前端和各个业务模块中。

本变更引入一个独立的 Agent 运行时，使通用短剧生产语义能够编译到不同的视频模型。Agent 必须服从现有 Cloud AI Gateway、模型路由、媒体权限、计费和 Provider Adapter，不直接持有供应商凭据，也不直接发起外部 HTTP 请求。

## Goals / Non-Goals

**Goals:**

- 在 `src/agents/` 建立与业务 API、模型适配器解耦的 Agent 运行时和 `short_drama` 工作流。
- 使用 LangGraph 编排有状态节点，并让节点状态可以在 PostgreSQL 中检查点保存、恢复和审计。
- 定义可序列化、可测试的 `AgentRunState`、`ShotPackage`、`AssetReference` 和 `ValidationReport` 契约。
- 将通用镜头生产包编译为模型 Profile 对应的 Prompt、参数和引用输入。
- 将 `@图片1` 等人类可读引用确定性绑定到授权的应用媒体 ID，再交给现有 Gateway/Provider Runtime。
- 在执行前提供阻断、警告、建议三级质检和人工确认节点。
- 保留 `seedance-short-drama` 作为通用 Skill 的兼容入口，并以 Profile 选择目标模型。

**Non-Goals:**

- 不在本变更中实现新的视频供应商 API Adapter；现有 Provider Adapter 仍是唯一外部调用边界。
- 不让 Agent 自主上传未经授权的素材、读取明文凭据、修改计费或绕过模型路由。
- 不把所有业务逻辑改造成多个相互对话的自治 Agent；短剧流程以单一有状态图和专用节点为主。
- 不承诺一条最终 Prompt 在所有模型上完全相同；通用的是语义结构，最终 Prompt 和参数由 Profile 编译。
- 不在本变更中解决视频生成质量的主观终审或自动化视频观感评估。

## Decisions

### 1. 使用 LangGraph 作为编排层，保留现有任务基础设施

`src/agents/core/runtime.py` 封装 LangGraph `StateGraph`，`src/agents/short_drama/graph.py` 定义短剧节点。FastAPI 只创建 Agent Run，Celery Worker 负责启动或恢复图执行；PostgreSQL 保存检查点和审计状态，Redis 继续承担队列和短期协调。

选择 LangGraph 而不是 CrewAI/AutoGen，是因为本流程需要显式顺序、条件分支、暂停、人审、恢复和节点级测试，而不是多角色自由对话。LangGraph 只负责图状态和节点跳转，不接管 Celery，也不直接调用供应商。

### 2. 使用结构化状态作为 Agent 节点边界

所有节点只接收和返回 Pydantic 定义的状态或补丁，禁止用未声明的长文本在节点之间传递关键事实。核心状态包含：

- `run_id`、`project_id`、请求幂等键和当前状态；
- `target_profile`、生成模式和模型路由快照；
- 一组 `ShotPackage`，包括镜头目的、起止状态、时间轴、动作、镜头、音频和引用；
- `AssetReference` 绑定结果及授权状态；
- `ValidationReport`、人工确认记录和已提交的视频任务 ID。

节点的外部副作用必须在提交节点集中执行，并通过现有服务的幂等键防止重复创建任务。失败节点记录结构化错误，允许从最近检查点恢复。

### 3. Profile 与模型目录分工

现有 `config/model_catalog/families/*.yaml` 继续作为模型能力、参数范围、输入数量和运行时路由的权威来源。新增的 Agent Profile 只描述通用镜头字段如何渲染为特定模型的 Prompt 片段、引用语义和参数映射，不复制凭据或路由配置。

Profile Resolver 必须先验证模型目录中的能力和数据库激活路由，再允许 Prompt Compiler 输出结果。Agent 可以提出目标模型，但最终选择必须由 `model_routing.py` 的服务端 allowlist 决定。

### 4. 素材引用分为语义层和执行层

Skill/Agent 使用统一的语义引用（例如 `@图片1` 及其用途说明）；Asset Binding 节点将其解析为带类型、职责、来源、授权状态和应用 `media_id` 的 `AssetReference`。云端请求只携带授权 `media_id`，由现有 Provider Input Resolver 生成签名 URL。

渲染后的 Prompt 必须保留“第几张参考图承担什么职责”的自然语言信息；引用标记可以作为编辑器标记，但不能只依赖被剥离的标签来传递语义。

### 5. 两阶段质检和人工确认

Quality Gate 先执行确定性检查：时间轴闭合、引用存在、模型能力、输入数量、素材授权和明显的镜头冲突；再可选调用 LLM Reviewer 解释风险或提出最小修订。阻断项禁止提交，警告项必须在人工确认或显式覆盖后才能提交，建议项不阻断。

### 6. 兼容入口策略

新增通用 Skill 标识 `short-drama-production`。`seedance-short-drama` 不立即删除，而是作为别名将请求转交给通用 Agent 并默认使用 Seedance Profile。未来 Grok、Wan、Kling 等只新增 Profile 和必要的 Provider 能力，不复制一套短剧流程。

## Risks / Trade-offs

- **[Risk] LangGraph 与 Celery 都保存状态，出现双重状态源。** → Celery 只保存队列/执行生命周期，Agent State 和检查点统一保存在 PostgreSQL；任务响应以 Agent Run ID 为准。
- **[Risk] LLM 节点输出不符合结构化协议。** → 所有 LLM 输出经过 Pydantic 校验和有限次重试；校验失败进入人工修订状态，不直接提交。
- **[Risk] Profile 与模型目录能力不一致。** → Profile 只引用模型 ID，运行时每次从激活目录解析能力并拒绝过期或未知 Profile。
- **[Risk] `@` 引用仍然无法找到应用媒体。** → Asset Binding 明确返回未解析引用和授权缺口；存在阻断项时不创建视频任务。
- **[Risk] Agent 选择了当前 PC 不开放的模型。** → 所有选择走服务端路由 allowlist；前端可展示推荐但不能绕过 `model_routing`。
- **[Risk] 增加 LangGraph 依赖和运维复杂度。** → 通过 `src/agents/core` 隔离框架 API；节点只依赖内部接口，未来可替换为轻量状态机而不改领域契约。

## Migration Plan

1. 先新增结构化契约、Profile Resolver、确定性 Validator 和 Agent 运行时骨架，不改变现有视频提交路径。
2. 将现有短剧润色、时间轴和素材解析逻辑包装为 Agent 节点，并通过兼容入口以 shadow/preview 模式运行。
3. 在测试环境启用人工确认和单模型 Profile，验证状态恢复、幂等、权限和计费链路。
4. 逐步将 PC 分镜生成按钮切换到 Agent Run；旧的直接视频任务入口保留作为回退，直到迁移完成。
5. 回滚时关闭 Agent 功能开关，恢复旧入口；已创建的 Provider 任务不撤销，按现有任务恢复策略继续处理。

## Open Questions

- 第一阶段是否只启用 Grok Profile，还是同时开放 Seedance Profile；这取决于 PC R2V 当前“仅 Grok”的产品约束是否解除。
- Seedance 生产最终走 Ark、MuleRouter，还是另行实现 Xlinks/NewAPI Adapter。
- 人工确认是否按镜头执行，还是允许整集一次性确认。
- Agent 检查点是否复用现有 AI 任务表，还是新增专用 Agent Run 表。
