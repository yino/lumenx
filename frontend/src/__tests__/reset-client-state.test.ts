// @vitest-environment happy-dom

import { beforeEach, describe, expect, it } from "vitest";

import { usePlaygroundStore } from "@/components/modules/playground/usePlaygroundStore";
import { resetClientStateForScope } from "@/store/resetClientState";
import { useProjectStore } from "@/store/projectStore";

describe("客户端内存状态清理", () => {
  beforeEach(() => {
    useProjectStore.getState().resetForScope();
    usePlaygroundStore.getState().resetForScope();
  });

  it("同时清空项目、系列、任务和 Playground 内容", () => {
    useProjectStore.setState({
      projects: [{ id: "project-1" } as never],
      currentProject: { id: "project-1" } as never,
      seriesList: [{ id: "series-1" } as never],
      currentSeries: { id: "series-1" } as never,
      generatingTasks: [{ assetId: "asset-1", generationType: "image", batchSize: 1 }],
      runningOps: { "task-1": true },
      selectedFrameId: "frame-1",
    });
    usePlaygroundStore.setState({
      prompt: "上一位用户的提示词",
      inputMedia: ["media-1"],
      history: [{ id: "generation-1" } as never],
      templates: [{ id: "template-1" } as never],
      activeGenerationIds: ["generation-1"],
      queue: [{ id: "queue-1" } as never],
      favoriteTemplateIds: ["template-1"],
    });

    resetClientStateForScope();

    expect(useProjectStore.getState()).toMatchObject({
      projects: [],
      currentProject: null,
      seriesList: [],
      currentSeries: null,
      generatingTasks: [],
      runningOps: {},
      selectedFrameId: null,
    });
    expect(usePlaygroundStore.getState()).toMatchObject({
      prompt: "",
      inputMedia: [],
      history: [],
      templates: [],
      activeGenerationIds: [],
      queue: [],
      favoriteTemplateIds: [],
    });
  });
});
