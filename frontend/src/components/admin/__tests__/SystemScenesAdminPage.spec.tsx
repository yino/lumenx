// @vitest-environment happy-dom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(),
  visibility: vi.fn(), archive: vi.fn(), reorder: vi.fn(), uploadMedia: vi.fn(), mediaAccess: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  systemSceneAdminApi: Object.fromEntries(Object.keys(mocks).map((key) => [key, (...args: unknown[]) => mocks[key as keyof typeof mocks](...args)])),
  getSafeApiError: (error: { code?: string; message?: string }) => ({ code: error.code, message: error.message || "操作失败" }),
}));

import SystemScenesAdminPage from "../SystemScenesAdminPage";

const scene = {
  id: "9", name: "雨夜街巷", description: "霓虹灯映照的雨夜街道", category: "城市", tags: ["雨夜"],
  prompt: "cinematic rainy street", negative_prompt: "", style: "赛博朋克", aspect_ratio: "16:9",
  visibility: "enabled", sort_order: 20, schema_version: 1, cover_media_id: null,
  version: 3, lifecycle: "active", usage_count: 2,
  created_at: "2026-08-16T00:00:00Z", updated_at: "2026-08-16T00:00:00Z", archived_at: null,
} as const;

describe("系统场景后台", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.list.mockResolvedValue({ items: [scene], total: 1, offset: 0, limit: 30 });
    mocks.get.mockResolvedValue(scene);
    mocks.visibility.mockResolvedValue({ ...scene, visibility: "disabled", version: 4 });
    mocks.reorder.mockResolvedValue({ ...scene, sort_order: 10, version: 4 });
  });

  it("在表单字段旁显示中文校验错误", async () => {
    render(<SystemScenesAdminPage />);
    await screen.findByText("雨夜街巷");
    fireEvent.click(screen.getByRole("button", { name: "新建场景" }));
    fireEvent.click(screen.getByRole("button", { name: "保存场景" }));

    expect(await screen.findByText("名称必须包含中文")).toBeInTheDocument();
    expect(screen.getByText("描述必须包含中文")).toBeInTheDocument();
    expect(screen.getByText("请填写场景分类")).toBeInTheDocument();
    expect(screen.getByText("请填写生成提示词")).toBeInTheDocument();
    expect(screen.getByText("操作原因必须包含中文")).toBeInTheDocument();
    expect(mocks.create).not.toHaveBeenCalled();
  });

  it("停用被引用场景时显示副本保护并提交确认数量", async () => {
    render(<SystemScenesAdminPage />);
    await screen.findByText("雨夜街巷");
    fireEvent.click(screen.getByRole("button", { name: "停用雨夜街巷" }));
    expect(screen.getByText(/当前已有 2 个用户副本/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("操作原因"), { target: { value: "运营下架过期系统场景" } });
    fireEvent.click(screen.getByRole("button", { name: "确认停用" }));
    await waitFor(() => expect(mocks.visibility).toHaveBeenCalledWith("9", "disabled", 3, "运营下架过期系统场景", 2));
  });

  it("排序操作使用当前版本并写入中文原因", async () => {
    render(<SystemScenesAdminPage />);
    await screen.findByText("雨夜街巷");
    fireEvent.click(screen.getByRole("button", { name: "向前排序雨夜街巷" }));
    expect(screen.getByText("排序值将从 20 调整为 10。")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("操作原因"), { target: { value: "运营调整推荐展示顺序" } });
    fireEvent.click(screen.getByRole("button", { name: "确认排序" }));
    await waitFor(() => expect(mocks.reorder).toHaveBeenCalledWith("9", 10, 3, "运营调整推荐展示顺序"));
  });
});
