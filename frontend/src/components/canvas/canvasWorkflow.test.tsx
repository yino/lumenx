import { describe, expect, it } from "vitest";

import {
  buildBatchTaskPlan,
  collectUpstreamTextInputs,
  resolveTaskDependencies,
  resolveUpstreamMedia,
  resolveUpstreamPrompt,
} from "@/components/canvas/canvasWorkflow";

const nodes = [
  { id: "note", data: { kind: "note", title: "需求", content: "做一个舞蹈视频" } },
  { id: "prompt", data: { kind: "prompt", title: "提示词", content: "成年女性舞者在摄影棚跳舞" } },
  { id: "task", data: { kind: "task", title: "生成任务", content: "" } },
];

describe("resolveUpstreamPrompt", () => {
  it("uses the connected prompt node as the generation input", () => {
    const result = resolveUpstreamPrompt(nodes, [
      { source: "note", target: "prompt" },
      { source: "prompt", target: "task" },
    ], "task");

    expect(result).toEqual({
      nodeId: "prompt",
      nodeTitle: "提示词",
      prompt: "成年女性舞者在摄影棚跳舞",
    });
  });

  it("falls back to an upstream text node when no prompt node is connected", () => {
    const result = resolveUpstreamPrompt(nodes, [
      { source: "note", target: "task" },
    ], "task");

    expect(result?.prompt).toBe("做一个舞蹈视频");
  });

  it("does not use unconnected content", () => {
    expect(resolveUpstreamPrompt(nodes, [], "task")).toBeNull();
  });

  it("prefers a connected composed prompt over raw upstream text", () => {
    const composedNodes = [
      ...nodes,
      { id: "composer", data: { kind: "composer", title: "合成提示词", content: "合并润色后的完整提示词" } },
    ];
    const result = resolveUpstreamPrompt(composedNodes, [
      { source: "note", target: "composer" },
      { source: "prompt", target: "composer" },
      { source: "composer", target: "task" },
    ], "task");

    expect(result).toEqual({
      nodeId: "composer",
      nodeTitle: "合成提示词",
      prompt: "合并润色后的完整提示词",
    });
  });
});

describe("collectUpstreamTextInputs", () => {
  it("collects all connected text inputs in connection order", () => {
    const composerNodes = [
      ...nodes,
      { id: "empty", data: { kind: "note", title: "空白", content: "   " } },
      { id: "composer", data: { kind: "composer", title: "合成提示词", content: "" } },
    ];
    const result = collectUpstreamTextInputs(composerNodes, [
      { source: "note", target: "composer" },
      { source: "prompt", target: "composer" },
      { source: "empty", target: "composer" },
    ], "composer");

    expect(result).toEqual([
      { nodeId: "note", nodeTitle: "需求", content: "做一个舞蹈视频" },
      { nodeId: "prompt", nodeTitle: "提示词", content: "成年女性舞者在摄影棚跳舞" },
    ]);
  });

  it("is cycle safe and does not include the composer itself", () => {
    const composerNodes = [
      { id: "note", data: { kind: "note", title: "需求", content: "月下追逐" } },
      { id: "composer", data: { kind: "composer", title: "合成提示词", content: "旧结果" } },
    ];
    const result = collectUpstreamTextInputs(composerNodes, [
      { source: "note", target: "composer" },
      { source: "composer", target: "note" },
    ], "composer");

    expect(result.map((item) => item.nodeId)).toEqual(["note"]);
  });
});

describe("resolveUpstreamMedia", () => {
  it("uses a directly connected image node", () => {
    const mediaNodes = [
      { id: "image", data: { kind: "image", title: "首帧", mediaUrl: "https://example.com/frame.png" } },
      { id: "task", data: { kind: "task", title: "图生视频" } },
    ];

    expect(resolveUpstreamMedia(mediaNodes, [{ source: "image", target: "task" }], "task", "image"))
      .toEqual({ nodeId: "image", nodeTitle: "首帧", mediaUrl: "https://example.com/frame.png" });
  });

  it("finds the generated output belonging to an upstream task", () => {
    const mediaNodes = [
      { id: "image-task", data: { kind: "task", title: "文生图" } },
      { id: "image-output", data: { kind: "image", title: "生成图", mediaUrl: "/frame.png", sourceTaskId: "image-task" } },
      { id: "video-task", data: { kind: "task", title: "图生视频" } },
    ];

    expect(resolveUpstreamMedia(
      mediaNodes,
      [{ source: "image-task", target: "video-task" }],
      "video-task",
      "image",
    )?.nodeId).toBe("image-output");
  });
});

describe("batch task planning", () => {
  const workflowNodes = [
    { id: "prompt", data: { kind: "prompt", title: "提示词", content: "跳舞" } },
    { id: "image-task", data: { kind: "task", title: "文生图" } },
    { id: "image-output", data: { kind: "image", title: "图片", sourceTaskId: "image-task" } },
    { id: "video-task", data: { kind: "task", title: "图生视频" } },
    { id: "parallel-task", data: { kind: "task", title: "文生视频" } },
  ];
  const workflowEdges = [
    { source: "prompt", target: "image-task" },
    { source: "image-task", target: "image-output" },
    { source: "image-output", target: "video-task" },
    { source: "prompt", target: "parallel-task" },
  ];

  it("discovers task dependencies through generated media nodes", () => {
    expect(resolveTaskDependencies(workflowNodes, workflowEdges, "video-task"))
      .toContain("image-task");
  });

  it("groups independent tasks and delays dependent tasks", () => {
    expect(buildBatchTaskPlan(workflowNodes, workflowEdges)).toEqual({
      layers: [["image-task", "parallel-task"], ["video-task"]],
      cyclicTaskIds: [],
    });
  });

  it("reports cyclic tasks instead of trying to run them", () => {
    const cyclic = workflowNodes.slice(0, 3);
    const plan = buildBatchTaskPlan(cyclic, [
      { source: "image-task", target: "image-output" },
      { source: "image-output", target: "image-task" },
    ]);
    expect(plan.cyclicTaskIds).toEqual(["image-task"]);
  });

  it("does not resubmit completed or already running tasks", () => {
    const plan = buildBatchTaskPlan([
      { id: "done", data: { kind: "task", title: "已完成", taskStatus: "done" } },
      { id: "running", data: { kind: "task", title: "执行中", taskStatus: "running" } },
      { id: "pending", data: { kind: "task", title: "待执行", taskStatus: "pending" } },
    ], []);
    expect(plan.layers).toEqual([["pending"]]);
  });
});
