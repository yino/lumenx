// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  select: vi.fn(),
  rename: vi.fn(),
  remove: vi.fn(),
  restore: vi.fn(),
  setActiveWorkspaceId: vi.fn(),
  setClientCacheScope: vi.fn(),
  clearClientCacheScope: vi.fn(),
  resetClientStateForScope: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  workspaceApi: {
    list: mocks.list,
    create: mocks.create,
    select: mocks.select,
    rename: mocks.rename,
    remove: mocks.remove,
    restore: mocks.restore,
  },
  setActiveWorkspaceId: mocks.setActiveWorkspaceId,
}));

vi.mock("@/lib/clientCacheScope", () => ({
  setClientCacheScope: mocks.setClientCacheScope,
  clearClientCacheScope: mocks.clearClientCacheScope,
}));

vi.mock("@/store/resetClientState", () => ({
  resetClientStateForScope: mocks.resetClientStateForScope,
}));

import { useWorkspaceStore } from "@/store/workspaceStore";

const primary = {
  id: "workspace-1",
  name: "默认工作区",
  version: 1,
  deleted: false,
  retention_expires_at: null,
};

const secondary = {
  id: "workspace-2",
  name: "新系列",
  version: 2,
  deleted: false,
  retention_expires_at: null,
};

describe("workspaceStore", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useWorkspaceStore.setState({
      status: "idle",
      workspaces: [],
      deletedWorkspaces: [],
      currentWorkspaceId: null,
      ownerUserId: null,
      busyWorkspaceId: null,
      error: null,
    });
  });

  it("初始化时选择用户的默认工作区并设置请求上下文", async () => {
    mocks.list.mockResolvedValue([primary, secondary]);
    mocks.select.mockResolvedValue(primary);

    await useWorkspaceStore.getState().initialize("user-1", primary.id);

    expect(mocks.list).toHaveBeenCalledWith(true);
    expect(mocks.select).toHaveBeenCalledWith(primary.id);
    expect(mocks.setActiveWorkspaceId).toHaveBeenLastCalledWith(primary.id);
    expect(mocks.setClientCacheScope).toHaveBeenCalledWith("user-1", primary.id);
    expect(mocks.resetClientStateForScope).toHaveBeenCalledTimes(2);
    expect(useWorkspaceStore.getState()).toMatchObject({
      status: "ready",
      currentWorkspaceId: primary.id,
      ownerUserId: "user-1",
      workspaces: [primary, secondary],
    });
  });

  it("切换工作区只更新服务端校验通过的上下文", async () => {
    useWorkspaceStore.setState({
      status: "ready",
      workspaces: [primary, secondary],
      currentWorkspaceId: primary.id,
      ownerUserId: "user-1",
    });
    mocks.select.mockResolvedValue(secondary);

    await useWorkspaceStore.getState().switchWorkspace(secondary.id);

    expect(mocks.select).toHaveBeenCalledWith(secondary.id);
    expect(mocks.setActiveWorkspaceId).toHaveBeenLastCalledWith(secondary.id);
    expect(useWorkspaceStore.getState().currentWorkspaceId).toBe(secondary.id);
  });

  it("删除当前工作区后切换到剩余工作区并保留回收记录", async () => {
    const deletedPrimary = {
      ...primary,
      version: 2,
      deleted: true,
      retention_expires_at: "2026-09-09T00:00:00Z",
    };
    useWorkspaceStore.setState({
      status: "ready",
      workspaces: [primary, secondary],
      currentWorkspaceId: primary.id,
      ownerUserId: "user-1",
    });
    mocks.remove.mockResolvedValue({ message: "已移入回收站" });
    mocks.list.mockResolvedValue([deletedPrimary, secondary]);
    mocks.select.mockResolvedValue(secondary);

    await useWorkspaceStore.getState().deleteWorkspace(primary.id);

    expect(mocks.remove).toHaveBeenCalledWith(primary.id);
    expect(mocks.select).toHaveBeenCalledWith(secondary.id);
    expect(useWorkspaceStore.getState()).toMatchObject({
      currentWorkspaceId: secondary.id,
      workspaces: [secondary],
      deletedWorkspaces: [deletedPrimary],
    });
  });

  it("重命名提交当前乐观锁版本", async () => {
    useWorkspaceStore.setState({
      status: "ready",
      workspaces: [primary],
      currentWorkspaceId: primary.id,
    });
    mocks.rename.mockResolvedValue({ ...primary, name: "短片工作区", version: 2 });

    await useWorkspaceStore.getState().renameWorkspace(primary.id, "短片工作区");

    expect(mocks.rename).toHaveBeenCalledWith(primary.id, "短片工作区", 1);
    expect(useWorkspaceStore.getState().workspaces[0]).toMatchObject({
      name: "短片工作区",
      version: 2,
    });
  });
});
