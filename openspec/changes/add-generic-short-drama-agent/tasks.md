## 1. Agent 基础与依赖

- [x] 1.1 在 Python 依赖中加入锁定版本的 LangGraph 运行时及 PostgreSQL checkpoint 支持，并完成最小导入检查
- [x] 1.2 创建 `src/agents/core/` 与 `src/agents/short_drama/` 包结构，定义公共工具、错误类型和模块导出
- [x] 1.3 使用 Pydantic 定义版本化的 `AgentRunState`、`ShotPackage`、`AssetReference`、`ValidationReport`、审批和任务状态契约
- [x] 1.4 为 Agent State 实现 JSON 序列化、schema 版本迁移、幂等键和敏感字段过滤
- [x] 1.5 增加 Agent 核心契约的单元测试，覆盖无效状态、未知阶段和非法状态迁移

## 2. 模型 Profile 与 Prompt 编译

- [x] 2.1 定义模型 Profile schema，区分模型能力/参数目录与 Prompt 渲染、引用语义配置
- [x] 2.2 实现 Profile Resolver，从激活的 model catalog 和数据库路由快照解析目标模型，不读取客户端凭据或绕过 allowlist
- [x] 2.3 实现通用 `ShotPackage` 到模型特定 Prompt、参数和有序输入引用的编译器
- [x] 2.4 为 Seedance 2.0 建立 Profile，并将现有 `seedance-short-drama` 映射为通用 Agent 的兼容别名
- [x] 2.5 为 `grok-imagine-video` 建立 Profile，声明 `images` 多图输入、时长范围和无模型音频能力
- [x] 2.6 将模型能力、时长、分辨率、参考图数量、音频和其他参数校验接入编译前检查
- [x] 2.7 为 Profile Resolver 和 Prompt Compiler 增加 Seedance/Grok、未知模型、禁用路由和超限参数测试

## 3. 素材引用与质量契约

- [x] 3.1 实现通用 `@图片N`、`@视频N`、`@音频N` 引用解析，并兼容 `[characterN:name]`、`[scene:name]`、`[prop:name]`
- [x] 3.2 实现 Asset Binding 节点，将引用绑定到角色、场景、道具和授权的应用 media/resource ID，保留类型、用途、来源和授权状态
- [x] 3.3 复用现有 Provider Input Resolver，将云端 media ID 转为授权签名 URL；未解析或未授权引用必须形成阻断项
- [x] 3.4 生成带语义的 reference map，保证标签从编辑器 Prompt 中移除后，模型输入仍保留“第几张图承担什么职责”的信息
- [x] 3.5 将 `validate_prompt.py` 的时间轴、引用和镜头冲突规则抽取为可调用的通用 Validator，并保留脚本兼容入口
- [x] 3.6 实现批量镜头连续性账本，检查人物、服装、道具状态、轴线、视线、光线、声音和首尾帧衔接
- [x] 3.7 增加安全/版权/真人授权检查和阻断、警告、建议三级结果映射
- [x] 3.8 为引用归一化、media 权限、时间轴闭合、连续性和安全结果补充单元及集成测试

## 4. LangGraph 短剧工作流

- [x] 4.1 在 `src/agents/short_drama/graph.py` 建立 `intake`、`storyboard_plan`、`continuity_check`、`asset_binding`、`prompt_compile`、`quality_gate`、`human_approval`、`submit_task`、`monitor_task` 节点图
- [x] 4.2 将现有剧本分析、分镜润色、Prompt Assembly 和时间轴逻辑包装为类型化 Agent 节点，保留现有函数和 API 的兼容调用
- [x] 4.3 为 LLM 节点增加结构化输出校验、有限次重试、模型回退和失败原因记录；校验失败不得直接提交任务
- [x] 4.4 实现 LangGraph PostgreSQL checkpoint 与独立 Agent Run 记录，保存阶段、状态版本、输入摘要、审批和审计事件
- [x] 4.5 实现节点副作用边界：提交节点只能调用内部视频任务/Gateway 工具，禁止 Agent 直接实例化 Provider 或发起外部 HTTP
- [x] 4.6 使用现有幂等键和 AI 任务 ID 关联，保证恢复或重复执行不会重复创建计费视频任务
- [x] 4.7 实现人工审批、拒绝、修订后重新校验和从检查点恢复的服务接口
- [x] 4.8 实现视频任务状态监听，将 Provider 任务状态同步回 Agent Run，并记录可查询的阶段事件
- [x] 4.9 为图节点顺序、条件分支、恢复、重复提交、审批和 Provider 策略边界增加集成测试

## 5. 后端 API 与云端集成

- [x] 5.1 增加创建 Agent Run、查询状态、获取生产包、审批/拒绝、恢复和取消的鉴权 API
- [x] 5.2 将 Agent Run 与 project、storyboard frame、AI task 和媒体资源建立作用域关系，遵守现有工作区隔离和 RLS 规则
- [x] 5.3 将 Agent 编译结果接入现有 Cloud AI Gateway 请求契约，禁止发送明文 URL、客户端模型覆盖和未授权媒体
- [x] 5.4 增加 Agent 运行日志、模型/Provider 快照、引用摘要、耗时和错误码，过滤 API key、签名 URL 和原始敏感内容
- [x] 5.5 保持现有 `/projects/{id}/video_tasks` 与润色接口可用；在迁移期允许通过 feature flag 选择旧路径或 Agent 路径
- [x] 5.6 增加 API、权限、幂等、数据库迁移和云端任务恢复测试

## 6. PC 分镜入口与 Skill 兼容

- [x] 6.1 在 PC 分镜工作台增加 Agent Run 状态、当前阶段、质检结果和人工审批状态展示
- [x] 6.2 将角色/场景/道具选择与 canonical asset reference 绑定；保留素材抽屉插入标签，同时支持从 Skill 生产包导入
- [x] 6.3 将模型选择改为读取服务端可用 Profile/路由；保留当前“R2V 仅 Grok”作为可配置 rollout 策略，不在客户端硬编码 Provider 行为
- [x] 6.4 新增 `skills/short-drama-production/` 通用 Skill 文档、Profile 使用说明和生产包导入格式
- [x] 6.5 更新 `skills/seedance-short-drama/` 为兼容别名，明确默认 Seedance Profile 和通用格式迁移说明
- [x] 6.6 增加 PC 端导入、质检阻断、审批、提交、轮询和旧入口回退的组件测试与端到端测试

## 7. 灰度、运维与回滚

- [x] 7.1 增加 Agent 功能开关、允许的 Profile 列表、审批策略和最大并发配置
- [x] 7.2 以 shadow/preview 模式对现有分镜生成运行 Agent 编译和质检，不创建 Provider 任务并比较结果
- [x] 7.3 在测试环境启用单 Profile 的端到端运行，验证检查点恢复、media 权限、计费和 Provider 输出处理
- [x] 7.4 增加 Agent Run、节点、Profile 和 Provider 快照的运维诊断与审计查询
- [x] 7.5 编写部署、依赖升级、数据库迁移和回滚说明；回滚时关闭 Agent 开关并保留已创建任务的现有恢复策略
- [x] 7.6 完成 LangGraph 依赖、数据库迁移、API、前端和 Provider 集成的发布前验证
