---
name: short-drama-production
description: 将短剧剧本和分镜整理为模型无关的结构化 ShotPackage，完成素材引用绑定、连续性质检、模型 Profile 编译和提交前人工确认。
---

# 通用短剧生产 Agent

这是 `src/agents/short_drama` 的产品入口。它只负责生产准备和编排，不直接调用供应商、不上传素材，也不接触 API key。

## 生产包格式

```json
{
  "schema_version": 1,
  "profile": "grok-imagine-video",
  "generation_mode": "r2v",
  "shots": [
    {
      "shot_id": "s01",
      "duration": 5,
      "purpose": "建立人物发现线索的紧张感",
      "subject": "角色 A，当前服装与发型保持连续",
      "scene": "夜间公寓客厅",
      "start_state": "角色 A 站在门边，右手悬在门把上方",
      "actions": ["听见敲门后收回手，视线转向猫眼"],
      "camera": "中近景，缓慢前推，保持右侧视线轴",
      "end_state": "手停在门把上方，尚未开门",
      "timeline": [{"start": 0, "end": 5, "action": "收回手并看向猫眼"}],
      "references": [{"alias": "image:1", "purpose": "角色外观"}]
    }
  ]
}
```

引用可使用 `@图片1`、`@视频1`、`@音频1`，迁移期间也接受 `[character1:张成]`、`[scene1:客厅]` 和 `[prop1:门把]`。引用必须在素材表中绑定应用内 `media_id`，不能填写 URL、文件路径或猜测的对象存储地址。

## Profile 选择

`seedance-short-drama` 是兼容别名，默认解析为 Seedance 2.0 Profile；新入口使用 `short-drama-production` 并由服务端 allowlist 解析 Profile。当前 PC R2V 灰度名单默认仅包含 `grok-imagine-video`，不要在客户端绕过路由开放其他模型。

Profile 编译会产生：清理后的模型 Prompt、有序 `media_ids`、`reference_map`（每张图的职责）和模型参数。编辑器标签可以被移除，但 reference map 必须保留。

## 质检与审批

提交前检查时间轴闭合、素材权限、模型能力、镜头冲突、连续性和安全/版权风险。结果使用 `blocking`、`warning`、`suggestion`：阻断项不能提交；警告项需显式人工审批；建议项不阻断。Worker 中断后从 PostgreSQL checkpoint 恢复，重复提交使用同一幂等键。

## API

- `POST /projects/{project_id}/agent-runs`
- `GET /projects/{project_id}/agent-runs/{run_id}`
- `GET /projects/{project_id}/agent-runs/{run_id}/production-package`
- `POST .../{run_id}/approve|reject|resume|cancel`

这些接口遵守现有工作区会话、CSRF、Cloud AI Gateway 和媒体权限边界。
