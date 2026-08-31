import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("static asset URLs", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("uses the OSS origin for bundled assets in cloud mode", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "cloud");
    const { getStaticAssetUrl } = await import("@/lib/staticAssets");

    expect(getStaticAssetUrl("/assets/templates/design-sheet.png")).toBe(
      "https://yino-drama.oss-cn-beijing.aliyuncs.com/assets/templates/design-sheet.png",
    );
  });

  it("preserves local asset paths in desktop mode", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "desktop");
    const { getStaticAssetUrl } = await import("@/lib/staticAssets");

    expect(getStaticAssetUrl("/assets/templates/design-sheet.png")).toBe(
      "/assets/templates/design-sheet.png",
    );
  });

  it("does not rewrite remote URLs or application routes", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "cloud");
    const { getStaticAssetUrl } = await import("@/lib/staticAssets");

    expect(getStaticAssetUrl("https://example.com/image.png")).toBe(
      "https://example.com/image.png",
    );
    expect(getStaticAssetUrl("/api/v1/projects/5/assets/generate")).toBe(
      "/api/v1/projects/5/assets/generate",
    );
  });

  it("supports an explicit cloud static asset origin", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "cloud");
    vi.stubEnv("NEXT_PUBLIC_STATIC_ASSET_BASE_URL", "https://static.example.com/");
    const { getStaticAssetUrl } = await import("@/lib/staticAssets");

    expect(getStaticAssetUrl("/assets/styles/example.webp")).toBe(
      "https://static.example.com/assets/styles/example.webp",
    );
  });
});
