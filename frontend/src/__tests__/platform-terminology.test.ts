import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const readSource = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

describe("平台中文术语", () => {
  it("认证、工作区、钱包和管理页面使用统一术语", () => {
    const source = [
      "src/components/auth/AuthScreen.tsx",
      "src/components/workspace/WorkspaceSwitcher.tsx",
      "src/components/wallet/WalletPage.tsx",
      "src/components/admin/TicketAdminPage.tsx",
      "src/components/admin/PlatformAdminPage.tsx",
      "src/components/admin/ConfigurationAdminPage.tsx",
    ].map(readSource).join("\n");

    expect(source).toContain("默认工作区");
    expect(source).toContain("可用算力券");
    expect(source).toContain("预扣算力券");
    expect(source).toContain("计量令牌");
    expect(source).toContain("计费待复核");
    expect(source).not.toContain("计量 token");
    expect(source).not.toContain("token / 券");
  });
});
