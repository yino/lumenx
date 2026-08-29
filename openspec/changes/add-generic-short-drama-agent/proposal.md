## Why

当前短剧生产规范绑定在 `seedance-short-drama` Skill 上，而实际生成链路又把模型差异、素材标签和任务执行逻辑分散在前端与各个 Provider 分支中。需要一个模型无关、可恢复、可人工确认的 Agent 编排层，让同一套短剧生产语义可以编译到 Seedance、Grok 以及后续视频模型，同时保留现有权限、路由、计费和任务执行边界。

## What Changes

- 新增通用 `short-drama-production` Agent 能力，覆盖剧本拆镜、连续性维护、素材绑定、提示词编译、质检和提交前人工确认。
- 将 Agent 运行时放入独立的 `src/agents/` 目录，与 `src/platform/video_providers/` 和业务 API 解耦。
- 使用结构化 `ShotPackage` / Agent State 作为节点之间的唯一契约，统一描述镜头目的、起止状态、时间轴、素材职责、声音和验收结果。
- 使用模型 Profile 描述不同模型的能力、参数、参考素材字段、音频能力和提示词渲染规则；模型差异不再散落在短剧业务流程中。
- 为 `@图片1` 等通用素材引用定义到应用内 `media_id` / Provider 输入的确定性绑定流程，并保留素材类型、用途和授权状态。
- 将 Seedance Skill 保留为兼容入口，改为调用通用 Agent 并默认选择 Seedance Profile。
- 将现有时间轴、引用、运镜冲突和安全/版权检查拆分为通用质检与模型特定质检，并在提交前形成可解释的阻断、警告和建议结果。
- 使用 LangGraph 作为可替换的 Agent 编排实现；继续复用现有 Celery、Redis、PostgreSQL、Cloud AI Gateway 和 Provider Adapter 执行实际任务。
- **BREAKING** 不允许 Agent 直接绕过模型路由、素材权限、参数校验、计费或 Provider Adapter 发起外部请求。

## Capabilities

### New Capabilities

- `short-drama-agent`: 提供通用短剧生产 Agent、结构化镜头生产包、节点编排、人工确认和可恢复执行。
- `model-profile-prompt-compilation`: 根据模型能力 Profile 将通用镜头生产包编译为模型特定 Prompt 和参数，同时校验能力与路由可用性。
- `short-drama-asset-quality-contract`: 统一素材引用、media ID 绑定、时间轴/连续性/安全质检和可解释验收结果。

### Modified Capabilities

<!-- No existing OpenSpec capability currently defines the short-drama Agent contract. -->

## Impact

- 新增 `src/agents/` 运行时包及其测试；可能新增 LangGraph 运行时依赖。
- 扩展现有分镜帧/视频任务的结构化字段或增加独立的 Agent 生产包存储，不改变 Provider API 的安全边界。
- 调整 `skills/seedance-short-drama/` 为通用 Skill 的兼容别名或配置入口，并新增通用 Skill/Profile 资源。
- 复用并适配 `src/platform/model_routing.py`、`src/platform/provider_runtime.py`、`src/platform/video_providers/`、Cloud AI Gateway 和现有异步任务状态。
- 前端分镜生成入口需要消费 Agent 状态、质检结果和人工确认状态；模型列表由目录/Profile 驱动，而不是硬编码 R2V 白名单。
- 需要补充 Agent 状态持久化、幂等、恢复、审计和结构化日志测试。
