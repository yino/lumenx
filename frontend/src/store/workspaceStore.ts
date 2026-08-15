import { create } from "zustand";

import {
  setActiveWorkspaceId,
  workspaceApi,
  type UserWorkspace,
} from "@/lib/api";
import {
  clearClientCacheScope,
  setClientCacheScope,
} from "@/lib/clientCacheScope";
import { resetClientStateForScope } from "@/store/resetClientState";

type WorkspaceStatus = "idle" | "loading" | "ready" | "error";

interface WorkspaceStore {
  status: WorkspaceStatus;
  workspaces: UserWorkspace[];
  deletedWorkspaces: UserWorkspace[];
  currentWorkspaceId: string | null;
  ownerUserId: string | null;
  busyWorkspaceId: string | null;
  error: string | null;
  initialize: (userId: string, defaultWorkspaceId: string) => Promise<void>;
  refresh: () => Promise<void>;
  createWorkspace: (name: string) => Promise<UserWorkspace>;
  switchWorkspace: (workspaceId: string) => Promise<UserWorkspace>;
  renameWorkspace: (workspaceId: string, name: string) => Promise<UserWorkspace>;
  deleteWorkspace: (workspaceId: string) => Promise<void>;
  restoreWorkspace: (workspaceId: string) => Promise<UserWorkspace>;
  reset: () => void;
}

function splitWorkspaces(records: UserWorkspace[]) {
  return {
    workspaces: records.filter((workspace) => !workspace.deleted),
    deletedWorkspaces: records.filter((workspace) => workspace.deleted),
  };
}

async function loadAllWorkspaces(): Promise<UserWorkspace[]> {
  return workspaceApi.list(true);
}

export const useWorkspaceStore = create<WorkspaceStore>((set, get) => ({
  status: "idle",
  workspaces: [],
  deletedWorkspaces: [],
  currentWorkspaceId: null,
  ownerUserId: null,
  busyWorkspaceId: null,
  error: null,

  initialize: async (userId, defaultWorkspaceId) => {
    setActiveWorkspaceId(null);
    clearClientCacheScope();
    resetClientStateForScope();
    set({
      status: "loading",
      workspaces: [],
      deletedWorkspaces: [],
      currentWorkspaceId: null,
      ownerUserId: userId,
      busyWorkspaceId: null,
      error: null,
    });
    try {
      const records = await loadAllWorkspaces();
      const active = records.filter((workspace) => !workspace.deleted);
      const target =
        active.find((workspace) => workspace.id === defaultWorkspaceId) ?? active[0];
      if (!target) throw new Error("当前账号没有可用工作区");
      const selected = await workspaceApi.select(target.id);
      setClientCacheScope(userId, selected.id);
      resetClientStateForScope();
      setActiveWorkspaceId(selected.id);
      set({
        ...splitWorkspaces(records),
        status: "ready",
        currentWorkspaceId: selected.id,
        ownerUserId: userId,
        error: null,
      });
    } catch (error) {
      setActiveWorkspaceId(null);
      clearClientCacheScope();
      resetClientStateForScope();
      set({
        status: "error",
        workspaces: [],
        deletedWorkspaces: [],
        currentWorkspaceId: null,
        ownerUserId: null,
        busyWorkspaceId: null,
        error: error instanceof Error ? error.message : "工作区加载失败",
      });
      throw error;
    }
  },

  refresh: async () => {
    const records = await loadAllWorkspaces();
    set({ ...splitWorkspaces(records), error: null });
  },

  createWorkspace: async (name) => {
    set({ busyWorkspaceId: "new", error: null });
    try {
      const created = await workspaceApi.create(name.trim());
      await workspaceApi.select(created.id);
      const records = await loadAllWorkspaces();
      const ownerUserId = get().ownerUserId;
      if (!ownerUserId) throw new Error("用户会话已失效");
      setClientCacheScope(ownerUserId, created.id);
      resetClientStateForScope();
      setActiveWorkspaceId(created.id);
      set({
        ...splitWorkspaces(records),
        currentWorkspaceId: created.id,
        busyWorkspaceId: null,
      });
      return created;
    } catch (error) {
      set({ busyWorkspaceId: null });
      throw error;
    }
  },

  switchWorkspace: async (workspaceId) => {
    if (workspaceId === get().currentWorkspaceId) {
      const current = get().workspaces.find((workspace) => workspace.id === workspaceId);
      if (!current) throw new Error("工作区不存在");
      return current;
    }
    set({ busyWorkspaceId: workspaceId, error: null });
    try {
      const selected = await workspaceApi.select(workspaceId);
      const ownerUserId = get().ownerUserId;
      if (!ownerUserId) throw new Error("用户会话已失效");
      setClientCacheScope(ownerUserId, selected.id);
      resetClientStateForScope();
      setActiveWorkspaceId(selected.id);
      set({ currentWorkspaceId: selected.id, busyWorkspaceId: null });
      return selected;
    } catch (error) {
      set({ busyWorkspaceId: null });
      throw error;
    }
  },

  renameWorkspace: async (workspaceId, name) => {
    const current = get().workspaces.find((workspace) => workspace.id === workspaceId);
    if (!current) throw new Error("工作区不存在");
    set({ busyWorkspaceId: workspaceId, error: null });
    try {
      const renamed = await workspaceApi.rename(workspaceId, name.trim(), current.version);
      set((state) => ({
        workspaces: state.workspaces.map((workspace) =>
          workspace.id === renamed.id ? renamed : workspace,
        ),
        busyWorkspaceId: null,
      }));
      return renamed;
    } catch (error) {
      set({ busyWorkspaceId: null });
      throw error;
    }
  },

  deleteWorkspace: async (workspaceId) => {
    set({ busyWorkspaceId: workspaceId, error: null });
    try {
      await workspaceApi.remove(workspaceId);
      const records = await loadAllWorkspaces();
      const split = splitWorkspaces(records);
      let nextWorkspaceId = get().currentWorkspaceId;
      if (nextWorkspaceId === workspaceId) {
        const replacement = split.workspaces[0];
        if (!replacement) throw new Error("当前账号没有可用工作区");
        await workspaceApi.select(replacement.id);
        nextWorkspaceId = replacement.id;
        const ownerUserId = get().ownerUserId;
        if (!ownerUserId) throw new Error("用户会话已失效");
        setClientCacheScope(ownerUserId, replacement.id);
        resetClientStateForScope();
        setActiveWorkspaceId(replacement.id);
      }
      set({
        ...split,
        currentWorkspaceId: nextWorkspaceId,
        busyWorkspaceId: null,
      });
    } catch (error) {
      set({ busyWorkspaceId: null });
      throw error;
    }
  },

  restoreWorkspace: async (workspaceId) => {
    set({ busyWorkspaceId: workspaceId, error: null });
    try {
      const restored = await workspaceApi.restore(workspaceId);
      const records = await loadAllWorkspaces();
      set({ ...splitWorkspaces(records), busyWorkspaceId: null });
      return restored;
    } catch (error) {
      set({ busyWorkspaceId: null });
      throw error;
    }
  },

  reset: () => {
    setActiveWorkspaceId(null);
    clearClientCacheScope();
    resetClientStateForScope();
    set({
      status: "idle",
      workspaces: [],
      deletedWorkspaces: [],
      currentWorkspaceId: null,
      ownerUserId: null,
      busyWorkspaceId: null,
      error: null,
    });
  },
}));
