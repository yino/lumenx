// @vitest-environment happy-dom

import { beforeEach, describe, expect, it } from "vitest";

import { useAuthStore } from "@/store/authStore";

describe("authStore", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.getState().beginSessionCheck();
  });

  it("只在内存中保存当前用户，不创建认证存储项", () => {
    useAuthStore.getState().setAuthenticated({
      id: "user-1",
      phone: "+8613800138000",
      phone_verified: false,
      phone_verification_status: "未验证",
      is_platform_admin: false,
      default_workspace_id: "workspace-1",
    });

    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(useAuthStore.getState().user?.phone).toBe("+8613800138000");
    expect(localStorage.length).toBe(0);
  });

  it("退出时同时清除用户和错误状态", () => {
    useAuthStore.getState().setAnonymous("登录状态已失效");

    expect(useAuthStore.getState()).toMatchObject({
      status: "anonymous",
      user: null,
      error: "登录状态已失效",
    });
  });
});
