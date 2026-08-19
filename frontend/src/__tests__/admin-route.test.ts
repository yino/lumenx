import { describe, expect, it } from "vitest";

import {
  ADMIN_SECTIONS,
  adminFilter,
  buildAdminHash,
  parseAdminRoute,
  parseAdminSection,
} from "@/lib/adminRoute";

describe("平台管理路由", () => {
  it("识别全部中文管理分区并兼容旧地址", () => {
    expect(ADMIN_SECTIONS).toEqual([
      "dashboard",
      "users",
      "invitations",
      "recharge-orders",
      "system-scenes",
      "resources",
      "exceptions",
      "tasks",
      "usage",
      "tickets",
      "configuration",
      "audit-events",
      "import-batches",
    ]);
    expect(parseAdminSection("#/admin")).toBe("dashboard");
    expect(parseAdminSection("#/admin/tickets")).toBe("tickets");
    expect(parseAdminSection("#/admin/configuration")).toBe("configuration");
    expect(parseAdminSection("#/admin/finance")).toBe("recharge-orders");
    expect(parseAdminSection("#/admin/not-supported")).toBe("dashboard");
    expect(parseAdminSection("#/settings")).toBeNull();
  });

  it("解析整数资源深链接和 URL 筛选参数", () => {
    expect(parseAdminRoute("#/admin/users/42")).toEqual({
      section: "users",
      resourceId: "42",
      queryString: "",
    });
    expect(parseAdminRoute("#/admin/recharge-orders?user_id=42&status=pending")).toEqual({
      section: "recharge-orders",
      resourceId: null,
      queryString: "user_id=42&status=pending",
    });
    expect(parseAdminRoute("#/admin/users/not-an-integer")).toEqual({
      section: "users",
      resourceId: null,
      queryString: "",
    });
  });

  it("稳定生成并读取可分享的筛选地址", () => {
    expect(buildAdminHash("users", {
      status: "active",
      user_id: "42",
      offset: 0,
      empty: "",
    })).toBe("#/admin/users?offset=0&status=active&user_id=42");
    expect(buildAdminHash("recharge-orders", { status: "pending" }, "9"))
      .toBe("#/admin/recharge-orders/9?status=pending");
    expect(adminFilter("status=active&user_id=42", "user_id")).toBe("42");
    expect(adminFilter("status=active", "missing")).toBe("");
  });
});
