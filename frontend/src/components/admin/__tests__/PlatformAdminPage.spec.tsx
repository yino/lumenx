// @vitest-environment happy-dom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listUsers: vi.fn(),
  createUser: vi.fn(),
  dashboard: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  adminPlatformApi: {
    listUsers: (...args: unknown[]) => mocks.listUsers(...args),
    createUser: (...args: unknown[]) => mocks.createUser(...args),
    dashboard: (...args: unknown[]) => mocks.dashboard(...args),
  },
  adminConfigurationApi: {},
  adminTicketApi: {},
  getSafeApiError: (error: { message?: string }) => ({
    message: error.message || "操作失败",
  }),
}));

import PlatformAdminPage from "../PlatformAdminPage";


describe("平台用户管理", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.listUsers.mockResolvedValue({
      items: [],
      total: 0,
      offset: 0,
      limit: 30,
    });
    mocks.createUser.mockResolvedValue({
      id: "2",
      workspace_id: "2",
      phone: "+8613800138088",
      status: "active",
    });
    mocks.dashboard.mockResolvedValue({
      generated_at: "2026-08-16T00:00:00Z",
      window: { start_at: "2026-08-15T00:00:00Z", end_at: "2026-08-16T00:00:00Z" },
      users: { total: 12, new: 2 },
      orders: { paid_count: 1, net_cash_fen: "9900", net_ticket_microtickets: "10000000" },
      usage: { events: 3, metering_tokens: "5000", charged_microtickets: "5000000" },
      tasks: { total: 4, by_status: { succeeded: 3 }, support_review: 0, failed: 1 },
      exceptions: { total: 0, items: [] },
    });
  });

  it("管理员可以填写手机号和初始密码添加用户", async () => {
    render(<PlatformAdminPage section="users" />);
    await screen.findByText("没有符合条件的用户");

    fireEvent.click(screen.getByRole("button", { name: "添加用户" }));
    fireEvent.change(screen.getByLabelText("手机号"), {
      target: { value: "13800138088" },
    });
    fireEvent.change(screen.getByLabelText("初始密码"), {
      target: { value: "secure-pass-2026" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "secure-pass-2026" },
    });
    fireEvent.change(screen.getByLabelText("创建原因"), {
      target: { value: "运营后台开户" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));

    await waitFor(() => {
      expect(mocks.createUser).toHaveBeenCalledWith(
        "+8613800138088",
        "secure-pass-2026",
        "运营后台开户",
      );
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(mocks.listUsers).toHaveBeenCalledTimes(2);
  });

  it("普通用户目录不承载管理员身份且不按同值 ID 自保护", async () => {
    mocks.listUsers.mockResolvedValue({
      items: [{
        id: "1",
        account_label: "138****8000",
        phone: "+8613800138000",
        status: "active",
        status_zh: "正常",
        phone_verified: false,
        available_tickets: "0",
        held_tickets: "0",
        wallet_exception: false,
        workspace_count: 1,
        task_summary: { total: 0, exceptions: 0 },
        created_at: "2026-08-16T00:00:00Z",
        updated_at: "2026-08-16T00:00:00Z",
      }],
      total: 1,
      offset: 0,
      limit: 30,
    });

    render(<PlatformAdminPage section="users" />);

    expect(await screen.findByText("138****8000")).toBeInTheDocument();
    expect(screen.queryByText("平台管理员")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停用" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "撤销会话" })).toBeEnabled();
  });

  it("默认仪表盘使用独立分组导航并提供移动端分区选择器", async () => {
    render(<PlatformAdminPage section="dashboard" />);

    expect(await screen.findByText("当前没有需要人工处理的异常")).toBeInTheDocument();
    expect(screen.getAllByText("漫屿AIGC 系统后台").length).toBeGreaterThan(0);
    expect(screen.getByRole("navigation", { name: "系统管理分区" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "选择系统管理分区" })).toHaveValue("dashboard");
    expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();
  });
});
