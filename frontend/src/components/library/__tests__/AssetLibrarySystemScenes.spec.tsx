// @vitest-environment happy-dom

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listSeries: vi.fn(),
  getProjects: vi.fn(),
  listLibraryAssets: vi.fn(),
  listSystemScenes: vi.fn(),
  copySystemScene: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, values?: { count?: number }) => values?.count === undefined ? key : `${key}:${values.count}`,
}));
vi.mock("@/lib/deployment", () => ({ IS_CLOUD_DEPLOYMENT: true }));
vi.mock("@/lib/api", () => ({
  api: {
    listSeries: (...args: unknown[]) => mocks.listSeries(...args),
    getProjects: (...args: unknown[]) => mocks.getProjects(...args),
    listLibraryAssets: (...args: unknown[]) => mocks.listLibraryAssets(...args),
  },
  systemSceneApi: {
    list: (...args: unknown[]) => mocks.listSystemScenes(...args),
    copy: (...args: unknown[]) => mocks.copySystemScene(...args),
    mediaAccess: vi.fn(),
  },
}));
vi.mock("@/store/workspaceStore", () => ({
  useWorkspaceStore: (selector: (state: { currentWorkspaceId: string }) => unknown) => selector({ currentWorkspaceId: "5" }),
}));
vi.mock("../AssetInspector", () => ({ default: () => null }));
vi.mock("../NewLibraryAssetDialog", () => ({ default: () => null }));

import AssetLibraryPage from "../AssetLibraryPage";

describe("普通用户系统场景目录", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.listSeries.mockResolvedValue([]);
    mocks.getProjects.mockResolvedValue([]);
    mocks.listLibraryAssets.mockResolvedValue({ characters: [], scenes: [], props: [] });
    mocks.listSystemScenes.mockResolvedValue([{
      id: "9", name: "雨夜街巷", description: "霓虹灯映照的雨夜街道", category: "城市", tags: ["雨夜"],
      prompt: "cinematic rainy street", negative_prompt: "", style: "赛博朋克", aspect_ratio: "16:9",
      visibility: "enabled", sort_order: 10, schema_version: 1, cover_media_id: null,
      version: 2, lifecycle: "active", usage_count: null,
      created_at: "2026-08-16T00:00:00Z", updated_at: "2026-08-16T00:00:00Z", archived_at: null,
    }]);
    mocks.copySystemScene.mockResolvedValue({ asset_record_id: "31", asset_id: "scene_copy", scope: "workspace", workspace_id: "5", source_system_scene_id: "9", source_version: 2, version: 1 });
  });

  it("只提供复制操作，并在复制成功后刷新当前工作区资产", async () => {
    render(<AssetLibraryPage />);
    const catalog = await screen.findByRole("region", { name: "系统场景目录" });
    expect(within(catalog).getByText("雨夜街巷")).toBeInTheDocument();
    expect(within(catalog).queryByRole("button", { name: /编辑|删除|归档|停用/ })).not.toBeInTheDocument();

    fireEvent.click(within(catalog).getByRole("button", { name: "复制雨夜街巷" }));
    await waitFor(() => expect(mocks.copySystemScene).toHaveBeenCalledWith("9"));
    await waitFor(() => expect(mocks.listLibraryAssets).toHaveBeenCalledTimes(2));
  });
});
