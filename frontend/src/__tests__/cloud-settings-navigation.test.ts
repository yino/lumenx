import { describe, expect, it } from "vitest";

import {
  normalizeSettingsCategory,
  visibleSettingsCategories,
} from "@/lib/settingsNavigation";

describe("云端设置导航", () => {
  it("不提供模型、供应商密钥、存储和系统设置入口", () => {
    expect(visibleSettingsCategories(true)).toEqual(["account", "general", "prompts"]);
    expect(visibleSettingsCategories(true)).not.toContain("models");
    expect(visibleSettingsCategories(true)).not.toContain("apikeys");
    expect(visibleSettingsCategories(true)).not.toContain("storage");
  });

  it("迁移旧模型分类到账号安全", () => {
    expect(normalizeSettingsCategory("models", true)).toBe("account");
    expect(normalizeSettingsCategory("models", false)).toBe("models");
  });
});
