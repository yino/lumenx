// @vitest-environment happy-dom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listUsers: vi.fn(),
  createUser: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  adminPlatformApi: {
    listUsers: (...args: unknown[]) => mocks.listUsers(...args),
    createUser: (...args: unknown[]) => mocks.createUser(...args),
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
});
