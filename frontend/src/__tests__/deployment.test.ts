import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("deployment model override policy", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("recursively removes browser-selected model routing in cloud mode", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "cloud");
    const { IS_CLOUD_DEPLOYMENT, withoutCloudModelOverrides } = await import(
      "@/lib/deployment"
    );
    const original = {
      prompt: "雨夜街道",
      model: "browser-model",
      parameters: {
        duration: 5,
        provider: "browser-provider",
        nested: [
          {
            model_id: "nested-model",
            model_name: "嵌套模型",
            provider_model_id: "provider-model",
            endpoint: "creative-value",
            DASHSCOPE_API_KEY: "secret-key",
            accessToken: "secret-token",
          },
        ],
      },
      model_settings: { i2v_model: "historical-model" },
      polish_model: "browser-llm",
      target_model: "browser-voice-model",
      default_model_id: "browser-template-model",
    };

    const sanitized = withoutCloudModelOverrides(original);

    expect(IS_CLOUD_DEPLOYMENT).toBe(true);
    expect(sanitized).toEqual({
      prompt: "雨夜街道",
      parameters: {
        duration: 5,
        nested: [{}],
      },
    });
    expect(original.model).toBe("browser-model");
    expect(original.parameters.nested[0].model_id).toBe("nested-model");
  });

  it("keeps desktop request payloads unchanged", async () => {
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "desktop");
    const { IS_CLOUD_DEPLOYMENT, withoutCloudModelOverrides } = await import(
      "@/lib/deployment"
    );
    const original = {
      model: "local-model",
      parameters: { provider: "local-provider", model_id: "local-model" },
    };

    const result = withoutCloudModelOverrides(original);

    expect(IS_CLOUD_DEPLOYMENT).toBe(false);
    expect(result).toBe(original);
    expect(result).toEqual(original);
  });
});
