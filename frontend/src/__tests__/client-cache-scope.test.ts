// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("客户端缓存作用域", () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("云端按用户和工作区隔离同名缓存", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "cloud");
    const {
      clientStorageKey,
      readClientStorage,
      setClientCacheScope,
      writeClientStorage,
    } = await import("@/lib/clientCacheScope");

    setClientCacheScope("user-1", "workspace-1");
    writeClientStorage("prompt-defaults", "用户一内容");
    expect(clientStorageKey("prompt-defaults")).toBe(
      "lumenx:user-1:workspace:workspace-1:prompt-defaults",
    );
    expect(readClientStorage("prompt-defaults")).toBe("用户一内容");

    setClientCacheScope("user-2", "workspace-1");
    expect(readClientStorage("prompt-defaults")).toBeNull();
    writeClientStorage("prompt-defaults", "用户二内容");

    setClientCacheScope("user-1", "workspace-2");
    expect(readClientStorage("prompt-defaults")).toBeNull();

    setClientCacheScope("user-1", "workspace-1");
    expect(readClientStorage("prompt-defaults")).toBe("用户一内容");
  });

  it("云端未建立认证作用域时不会读取旧版全局缓存", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "cloud");
    localStorage.setItem("lumenx_default_prompt_config", "旧用户内容");
    const { readClientStorage, writeClientStorage } = await import("@/lib/clientCacheScope");

    expect(readClientStorage("lumenx_default_prompt_config")).toBeNull();
    writeClientStorage("lumenx_default_prompt_config", "新内容");
    expect(localStorage.getItem("lumenx_default_prompt_config")).toBe("旧用户内容");
  });

  it("桌面模式继续使用原有本地键", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "desktop");
    const { clientStorageKey, readClientStorage, writeClientStorage } = await import("@/lib/clientCacheScope");

    writeClientStorage("project-layout", "gallery");

    expect(clientStorageKey("project-layout")).toBe("project-layout");
    expect(readClientStorage("project-layout")).toBe("gallery");
  });
});
