import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  getSafeApiError: (error: { message?: string }) => ({
    message: error.message || "操作失败",
  }),
}));

vi.mock("@/lib/clientCacheScope", () => ({
  setClientCacheScope: mocks.setClientCacheScope,
  clearClientCacheScope: mocks.clearClientCacheScope,
}));

vi.mock("@/store/resetClientState", () => ({
  resetClientStateForScope: mocks.resetClientStateForScope,
}));

import { useWorkspaceStore } from "@/store/workspaceStore";
import WorkspaceSwitcher from "../WorkspaceSwitcher";

const primary = {
  id: "workspace-1",
  name: "默认工作区",
  version: 1,
  deleted: false,
  retention_expires_at: null,
};

const secondary = {
  id: "workspace-2",
  name: "短片工作区",
  version: 1,
  deleted: false,
  retention_expires_at: null,
};

const archived = {
  id: "workspace-old",
  name: "旧工作区",
  version: 3,
  deleted: true,
  retention_expires_at: "2026-09-09T00:00:00Z",
};

describe("WorkspaceSwitcher", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.location.hash = "#/settings";
    useWorkspaceStore.setState({
      status: "ready",
      workspaces: [primary, secondary],
      deletedWorkspaces: [archived],
      currentWorkspaceId: primary.id,
      ownerUserId: "user-1",
      busyWorkspaceId: null,
      error: null,
    });
  });

  it("显示当前工作区并在切换后回到项目页", async () => {
    mocks.select.mockResolvedValue(secondary);
    render(<WorkspaceSwitcher />);

    fireEvent.click(screen.getByRole("button", { expanded: false }));
    fireEvent.click(screen.getByRole("button", { name: secondary.name }));

    await waitFor(() => {
      expect(mocks.select).toHaveBeenCalledWith(secondary.id);
      expect(useWorkspaceStore.getState().currentWorkspaceId).toBe(secondary.id);
    });
    expect(window.location.hash).toBe("#/");
  });

  it("从回收站恢复工作区", async () => {
    const restored = { ...archived, deleted: false, retention_expires_at: null };
    mocks.restore.mockResolvedValue(restored);
    mocks.list.mockResolvedValue([primary, secondary, restored]);
    render(<WorkspaceSwitcher />);

    fireEvent.click(screen.getByRole("button", { expanded: false }));
    fireEvent.click(screen.getByRole("button", { name: /回收站/ }));
    fireEvent.click(screen.getByRole("button", { name: `恢复${archived.name}` }));

    await waitFor(() => {
      expect(mocks.restore).toHaveBeenCalledWith(archived.id);
      expect(useWorkspaceStore.getState().deletedWorkspaces).toEqual([]);
    });
  });
});
