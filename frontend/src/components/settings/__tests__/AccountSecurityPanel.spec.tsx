import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockListSessions = vi.fn();
const mockChangePassword = vi.fn();
const mockRevokeSession = vi.fn();
const mockRevokeAllSessions = vi.fn();
const mockLogout = vi.fn();

vi.mock("@/lib/api", () => ({
  authApi: {
    listSessions: (...args: unknown[]) => mockListSessions(...args),
    changePassword: (...args: unknown[]) => mockChangePassword(...args),
    revokeSession: (...args: unknown[]) => mockRevokeSession(...args),
    revokeAllSessions: (...args: unknown[]) => mockRevokeAllSessions(...args),
    logout: (...args: unknown[]) => mockLogout(...args),
  },
  getSafeApiError: (error: { message?: string }) => ({
    message: error.message || "操作失败",
  }),
}));

import { useAuthStore } from "@/store/authStore";
import AccountSecurityPanel from "../AccountSecurityPanel";

const user = {
  id: "user-1",
  phone: "+8613800138000",
  account_label: "138****8000",
  phone_verified: false,
  phone_verification_status: "未验证",
  default_workspace_id: "workspace-1",
};

const sessions = [
  {
    id: "session-current",
    current: true,
    created_at: "2026-08-10T00:00:00Z",
    last_seen_at: "2026-08-10T01:00:00Z",
    idle_expires_at: "2026-08-17T01:00:00Z",
    absolute_expires_at: "2026-09-10T00:00:00Z",
    revoked_at: null,
    user_agent: "当前浏览器",
  },
  {
    id: "session-other",
    current: false,
    created_at: "2026-08-09T00:00:00Z",
    last_seen_at: "2026-08-09T01:00:00Z",
    idle_expires_at: "2026-08-16T01:00:00Z",
    absolute_expires_at: "2026-09-09T00:00:00Z",
    revoked_at: null,
    user_agent: "其他浏览器",
  },
];

describe("AccountSecurityPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListSessions.mockResolvedValue(sessions);
    useAuthStore.getState().setAuthenticated(user);
  });

  it("显示脱敏手机号、验证状态和登录设备", async () => {
    render(<AccountSecurityPanel />);

    expect(screen.getByText("+86 138 **** 8000")).toBeInTheDocument();
    expect(screen.getByText("未验证")).toBeInTheDocument();
    expect(await screen.findByText("当前浏览器")).toBeInTheDocument();
    expect(screen.getByText("其他浏览器")).toBeInTheDocument();
    expect(screen.getByText("当前设备")).toBeInTheDocument();
  });

  it("在客户端阻止不符合策略的新密码", async () => {
    render(<AccountSecurityPanel />);

    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "old-password1" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "short1" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "short1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "更新密码" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("新密码至少需要 10 位");
    expect(mockChangePassword).not.toHaveBeenCalled();
  });

  it("修改密码时调用服务端且不持久化密码", async () => {
    mockChangePassword.mockResolvedValue({ message: "密码已更新，其他设备已退出登录" });
    render(<AccountSecurityPanel />);

    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "old-password1" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "new-password2" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "new-password2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "更新密码" }));

    await waitFor(() => {
      expect(mockChangePassword).toHaveBeenCalledWith("old-password1", "new-password2");
    });
    expect(localStorage.getItem("lumenx-auth")).toBeNull();
  });

  it("可以撤销其他设备会话", async () => {
    mockRevokeSession.mockResolvedValue({ message: "会话已撤销" });
    render(<AccountSecurityPanel />);
    await screen.findByText("其他浏览器");

    fireEvent.click(screen.getByRole("button", { name: "撤销该设备" }));

    await waitFor(() => {
      expect(mockRevokeSession).toHaveBeenCalledWith("session-other");
    });
  });

  it("服务端退出成功后清空内存会话", async () => {
    mockLogout.mockResolvedValue({ message: "已退出登录" });
    render(<AccountSecurityPanel />);

    fireEvent.click(screen.getByRole("button", { name: "退出当前账号" }));

    await waitFor(() => {
      expect(useAuthStore.getState().status).toBe("anonymous");
    });
    expect(useAuthStore.getState().user).toBeNull();
  });
});
