import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockCurrentUser = vi.fn();
const mockLogin = vi.fn();
const mockRegister = vi.fn();
const mockRegistrationPolicy = vi.fn();

vi.mock("@/lib/deployment", () => ({
  IS_CLOUD_DEPLOYMENT: true,
}));

vi.mock("@/lib/api", () => ({
  SESSION_EXPIRED_EVENT: "lumenx:session-expired",
  authApi: {
    currentUser: (...args: unknown[]) => mockCurrentUser(...args),
    login: (...args: unknown[]) => mockLogin(...args),
    register: (...args: unknown[]) => mockRegister(...args),
    registrationPolicy: (...args: unknown[]) => mockRegistrationPolicy(...args),
  },
  getSafeAuthError: (error: { code?: string; message?: string }) => ({
    code: error.code,
    message: error.message || "网络连接异常，请检查网络后重试",
  }),
}));

import AuthGate from "../AuthGate";

const user = {
  id: "user-1",
  phone: "+8613800138000",
  phone_verified: false,
  phone_verification_status: "未验证",
  is_platform_admin: false,
  default_workspace_id: "workspace-1",
};

describe("AuthGate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRegistrationPolicy.mockResolvedValue({
      mode: "invite_only",
      verification_available: false,
    });
  });

  it("未恢复会话时不挂载受保护内容", async () => {
    mockCurrentUser.mockRejectedValue({ code: "AUTH_REQUIRED", message: "请先登录" });

    render(
      <AuthGate>
        <div>受保护的创作台</div>
      </AuthGate>,
    );

    expect(screen.getByText("正在恢复创作现场")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "欢迎回来" })).toBeInTheDocument();
    expect(screen.queryByText("受保护的创作台")).not.toBeInTheDocument();
  });

  it("有效会话恢复后挂载受保护内容", async () => {
    mockCurrentUser.mockResolvedValue({ user });

    render(
      <AuthGate>
        <div>受保护的创作台</div>
      </AuthGate>,
    );

    expect(await screen.findByText("受保护的创作台")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "欢迎回来" })).not.toBeInTheDocument();
  });

  it("账号停用时显示中文状态且不挂载受保护内容", async () => {
    mockCurrentUser.mockRejectedValue({
      code: "ACCOUNT_SUSPENDED",
      message: "账号已停用，请联系平台管理员",
    });

    render(
      <AuthGate>
        <div>受保护的创作台</div>
      </AuthGate>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "账号已停用，请联系平台管理员",
    );
    expect(screen.queryByText("受保护的创作台")).not.toBeInTheDocument();
  });

  it("使用 +86 规范手机号登录并进入创作台", async () => {
    mockCurrentUser.mockRejectedValue({ code: "AUTH_REQUIRED", message: "请先登录" });
    mockLogin.mockResolvedValue({ user });

    render(
      <AuthGate>
        <div>受保护的创作台</div>
      </AuthGate>,
    );

    fireEvent.change(await screen.findByLabelText("手机号"), {
      target: { value: "13800138000" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "storypass1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(mockLogin).toHaveBeenCalledWith("+8613800138000", "storypass1");
    });
    expect(await screen.findByText("受保护的创作台")).toBeInTheDocument();
  });

  it("注册时披露手机号未验证状态并执行本地密码校验", async () => {
    mockCurrentUser.mockRejectedValue({ code: "AUTH_REQUIRED", message: "请先登录" });

    render(
      <AuthGate>
        <div>受保护的创作台</div>
      </AuthGate>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "注册" }));
    expect(screen.getByText(/手机号尚未经过短信验证/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("手机号"), {
      target: { value: "13800138000" },
    });
    fireEvent.change(screen.getByLabelText("邀请码"), {
      target: { value: "invite-short-password" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "short1" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "short1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("密码至少需要 10 位");
    expect(mockRegister).not.toHaveBeenCalled();
  });

  it("注册成功后直接进入创作台", async () => {
    mockCurrentUser.mockRejectedValue({ code: "AUTH_REQUIRED", message: "请先登录" });
    mockRegister.mockResolvedValue({ user });

    render(
      <AuthGate>
        <div>受保护的创作台</div>
      </AuthGate>,
    );

    fireEvent.click(await screen.findByRole("tab", { name: "注册" }));
    fireEvent.change(screen.getByLabelText("手机号"), {
      target: { value: "13800138000" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "storypass1" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "storypass1" },
    });
    fireEvent.change(screen.getByLabelText("邀请码"), {
      target: { value: "invite-success" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }));

    await waitFor(() => {
      expect(mockRegister).toHaveBeenCalledWith(
        "+8613800138000",
        "storypass1",
        "invite-success",
      );
    });
    expect(await screen.findByText("受保护的创作台")).toBeInTheDocument();
  });
});
