// @vitest-environment happy-dom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  currentAdmin: vi.fn(),
  login: vi.fn(),
  changePassword: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  ADMIN_SESSION_EXPIRED_EVENT: "lumenx:admin-session-expired",
  adminAuthApi: {
    currentAdmin: (...args: unknown[]) => mocks.currentAdmin(...args),
    login: (...args: unknown[]) => mocks.login(...args),
    changePassword: (...args: unknown[]) => mocks.changePassword(...args),
    logout: (...args: unknown[]) => mocks.logout(...args),
  },
  getSafeApiError: (error: { message?: string }) => ({
    message: error.message || "网络连接异常，请检查网络后重试",
  }),
}));

import AdminAuthGate from "../AdminAuthGate";
import { useAdminAuthStore } from "@/store/adminAuthStore";

const admin = {
  id: "1",
  username: "admin",
  must_change_password: false,
};

describe("独立管理员认证门禁", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAdminAuthStore.setState({
      status: "checking",
      admin: null,
      error: null,
    });
  });

  it("管理员会话恢复前不挂载后台内容", async () => {
    mocks.currentAdmin.mockRejectedValue({ message: "请先登录系统管理后台" });

    render(
      <AdminAuthGate>
        <div>系统后台内容</div>
      </AdminAuthGate>,
    );

    expect(screen.getByText("正在验证管理员会话")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "管理员登录" })).toBeInTheDocument();
    expect(screen.queryByText("系统后台内容")).not.toBeInTheDocument();
  });

  it("仅使用管理员凭据登录并挂载后台内容", async () => {
    mocks.currentAdmin.mockRejectedValue({ message: "请先登录系统管理后台" });
    mocks.login.mockResolvedValue({ admin });

    render(
      <AdminAuthGate>
        <div>系统后台内容</div>
      </AdminAuthGate>,
    );

    fireEvent.change(await screen.findByLabelText("管理员账号"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "SecureAdminPass123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "进入系统后台" }));

    await waitFor(() => {
      expect(mocks.login).toHaveBeenCalledWith("admin", "SecureAdminPass123");
    });
    expect(await screen.findByText("系统后台内容")).toBeInTheDocument();
  });

  it("独立管理员会话恢复后直接挂载后台内容", async () => {
    mocks.currentAdmin.mockResolvedValue({ admin });

    render(
      <AdminAuthGate>
        <div>系统后台内容</div>
      </AdminAuthGate>,
    );

    expect(await screen.findByText("系统后台内容")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "管理员登录" })).not.toBeInTheDocument();
  });

  it("初始密码未修改时阻止挂载后台并完成强制修改", async () => {
    mocks.currentAdmin.mockResolvedValue({
      admin: { ...admin, must_change_password: true },
    });
    mocks.changePassword.mockResolvedValue({ message: "管理员密码已更新" });

    render(
      <AdminAuthGate>
        <div>系统后台内容</div>
      </AdminAuthGate>,
    );

    expect(await screen.findByRole("heading", { name: "修改初始管理员密码" })).toBeInTheDocument();
    expect(screen.queryByText("系统后台内容")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "InitialAdminPass123" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "RotatedAdminPass123" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "RotatedAdminPass123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "修改并进入后台" }));

    await waitFor(() => {
      expect(mocks.changePassword).toHaveBeenCalledWith(
        "InitialAdminPass123",
        "RotatedAdminPass123",
      );
    });
    expect(await screen.findByText("系统后台内容")).toBeInTheDocument();
  });

  it("管理员会话失效事件立即卸载后台内容", async () => {
    mocks.currentAdmin.mockResolvedValue({ admin });
    render(
      <AdminAuthGate>
        <div>系统后台内容</div>
      </AdminAuthGate>,
    );
    await screen.findByText("系统后台内容");

    window.dispatchEvent(
      new CustomEvent("lumenx:admin-session-expired", {
        detail: { message: "管理员登录状态已失效，请重新登录" },
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "管理员登录状态已失效，请重新登录",
    );
    expect(screen.queryByText("系统后台内容")).not.toBeInTheDocument();
  });
});
