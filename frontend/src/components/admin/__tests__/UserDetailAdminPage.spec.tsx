// @vitest-environment happy-dom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  userOverview: vi.fn(),
  userResources: vi.fn(),
  viewScript: vi.fn(),
  viewTaskPrompt: vi.fn(),
  previewMedia: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  adminPlatformApi: {
    userOverview: (...args: unknown[]) => mocks.userOverview(...args),
    userResources: (...args: unknown[]) => mocks.userResources(...args),
    viewScript: (...args: unknown[]) => mocks.viewScript(...args),
    viewTaskPrompt: (...args: unknown[]) => mocks.viewTaskPrompt(...args),
    previewMedia: (...args: unknown[]) => mocks.previewMedia(...args),
  },
  getSafeApiError: (error: { message?: string }) => ({
    message: error.message || "操作失败",
  }),
}));

import UserDetailAdminPage from "../UserDetailAdminPage";

describe("管理员用户详情", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.userOverview.mockResolvedValue({
      account: {
        id: "42",
        username: null,
        phone_masked: "+86138****8000",
        status: "active",
        phone_verified: false,
        created_at: "2026-08-16T00:00:00Z",
        updated_at: "2026-08-16T00:00:00Z",
      },
      wallet: {
        available_microtickets: "4000000",
        available_tickets: "4",
        held_microtickets: "1000000",
        held_tickets: "1",
        lifetime_recharged_microtickets: "3000000",
        lifetime_refunded_microtickets: "0",
      },
      counts: { workspaces: 1, projects: 2, series: 1, assets: 4, media: 3, tasks: 1, usage: 1, orders: 1 },
      exceptions: { tasks: 1, orders: 0, total: 1 },
      recent_activity: {
        tasks: [{ id: "71", workspace_id: "9", capability: "图像生成", status: "support_review", updated_at: "2026-08-16T01:00:00Z" }],
        orders: [{ id: "81", order_number: "MR202608160001", status: "completed", status_zh: "已完成", updated_at: "2026-08-16T02:00:00Z" }],
      },
    });
    mocks.userResources.mockImplementation((_userId: string, resource: string) => Promise.resolve({
      items: resource === "workspaces"
        ? [{ id: "9", name: "默认工作区" }]
        : resource === "projects"
          ? [{ id: "51", workspace_id: "9", title: "隐私剧本", status: "active", updated_at: "2026-08-16T03:00:00Z" }]
          : [],
      total: 1,
      offset: 0,
      limit: 30,
    }));
    mocks.viewScript.mockResolvedValue({
      project_id: "51",
      title: "隐私剧本",
      text: "这是审计后返回的完整剧本",
      truncated: false,
    });
  });

  it("展示运营概览并仅在切换标签后加载任务分页", async () => {
    render(<UserDetailAdminPage userId="42" />);

    expect(await screen.findByText("最近活动")).toBeInTheDocument();
    expect(screen.getByText("+86138****8000")).toBeInTheDocument();
    expect(screen.getByText("MR202608160001")).toBeInTheDocument();
    expect(mocks.userResources).not.toHaveBeenCalledWith("42", "tasks", expect.anything());

    fireEvent.click(screen.getByRole("tab", { name: "AI 任务" }));

    await waitFor(() => {
      expect(mocks.userResources).toHaveBeenCalledWith("42", "tasks", expect.objectContaining({
        offset: 0,
        limit: 30,
      }));
    });
  });

  it("敏感剧本必须填写中文用途后才允许查看", async () => {
    render(<UserDetailAdminPage userId="42" />);
    await screen.findByText("最近活动");

    fireEvent.click(screen.getByRole("tab", { name: "项目与剧本" }));
    await screen.findByText("隐私剧本");
    fireEvent.click(screen.getByRole("button", { name: "查看剧本" }));
    fireEvent.click(screen.getByRole("button", { name: "审计并查看" }));

    expect(await screen.findByText("操作原因必须包含中文说明")).toBeInTheDocument();
    expect(mocks.viewScript).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服排查用户反馈的剧本问题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "审计并查看" }));

    expect(await screen.findByText("这是审计后返回的完整剧本")).toBeInTheDocument();
    expect(mocks.viewScript).toHaveBeenCalledWith(
      "42",
      "51",
      "9",
      "客服排查用户反馈的剧本问题",
    );
    expect(screen.queryByText(/object_key/i)).not.toBeInTheDocument();
  });
});
