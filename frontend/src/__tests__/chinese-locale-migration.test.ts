// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from "vitest";

describe("中文 locale 迁移", () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });

  it("将旧版英文偏好迁移为中文且不再持久化 locale", async () => {
    localStorage.setItem(
      "lumenx-settings",
      JSON.stringify({
        state: {
          locale: "en",
          theme: "brand-dark",
          animations: false,
        },
        version: 1,
      }),
    );

    const { useSettingsStore } = await import("@/store/settingsStore");
    await useSettingsStore.persist.rehydrate();

    expect(useSettingsStore.getState()).toMatchObject({
      locale: "zh",
      theme: "brand-dark",
      animations: false,
    });
    useSettingsStore.getState().setTheme("atelier-dark");
    const persisted = JSON.parse(String(localStorage.getItem("lumenx-settings")));
    expect(persisted.state.locale).toBeUndefined();
  });
});
