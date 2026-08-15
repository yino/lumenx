import { describe, expect, it } from "vitest";

import {
  ADMIN_SECTIONS,
  canAccessAdminRoute,
  parseAdminSection,
} from "@/lib/adminRoute";

describe("平台管理路由", () => {
  it("识别全部中文管理分区并兼容旧地址", () => {
    expect(ADMIN_SECTIONS).toEqual([
      "users",
      "invitations",
      "tasks",
      "usage",
      "tickets",
      "configuration",
      "audit-events",
      "import-batches",
    ]);
    expect(parseAdminSection("#/admin")).toBe("users");
    expect(parseAdminSection("#/admin/tickets")).toBe("tickets");
    expect(parseAdminSection("#/admin/configuration")).toBe("configuration");
    expect(parseAdminSection("#/admin/not-supported")).toBe("users");
    expect(parseAdminSection("#/settings")).toBeNull();
  });

  it("只有平台管理员可进入管理路由", () => {
    expect(canAccessAdminRoute(true)).toBe(true);
    expect(canAccessAdminRoute(false)).toBe(false);
  });
});
