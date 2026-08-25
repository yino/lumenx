// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AxiosError, AxiosHeaders, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";

interface CapturedRequest {
  url?: string;
  withCredentials?: boolean;
  csrf?: string;
  authorization?: string;
  apiKey?: string;
  workspaceId?: string;
  ifMatch?: string;
  idempotencyKey?: string;
  timeout?: number;
  data: unknown;
}

function captureAdapter(target: CapturedRequest[]) {
  return async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
    const data = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
    target.push({
      url: config.url,
      withCredentials: config.withCredentials,
      csrf: config.headers.get("X-CSRF-Token") as string | undefined,
      authorization: config.headers.get("Authorization") as string | undefined,
      apiKey: config.headers.get("X-API-Key") as string | undefined,
      workspaceId: config.headers.get("X-Workspace-ID") as string | undefined,
      ifMatch: config.headers.get("If-Match") as string | undefined,
      idempotencyKey: config.headers.get("Idempotency-Key") as string | undefined,
      timeout: config.timeout,
      data,
    });
    return {
      config,
      data: { ok: true },
      headers: new AxiosHeaders(),
      status: 200,
      statusText: "OK",
    };
  };
}

describe("云端 API 客户端", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_DEPLOYMENT_MODE", "cloud");
    document.cookie = "lumenx_csrf=csrf-test; path=/";
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
    document.cookie = "lumenx_csrf=; Max-Age=0; path=/";
  });

  it("所有请求携带 cookie，变更请求自动附加 CSRF", async () => {
    const { API_URL, apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");

    await apiClient.post("/projects", { title: "测试项目" }, {
      adapter: captureAdapter(captured),
    });

    expect(API_URL).toBe(`${window.location.origin}/api/v1`);
    expect(apiClient.defaults.withCredentials).toBe(true);
    expect(captured[0]).toMatchObject({
      withCredentials: true,
      csrf: "csrf-test",
      workspaceId: "workspace-1",
      data: { title: "测试项目" },
    });
  });

  it("普通云端请求剥离模型、供应商、端点和明文凭据", async () => {
    const { apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];

    await apiClient.post(
      "/playground/generate",
      {
        prompt: "雨夜街道",
        model_id: "browser-model",
        provider: "browser-provider",
        endpoint: "https://provider.invalid",
        DASHSCOPE_API_KEY: "secret",
        parameters: {
          duration: 5,
          secretKey: "nested-secret",
          target_model: "browser-voice",
        },
      },
      {
        adapter: captureAdapter(captured),
        headers: {
          Authorization: "Bearer secret",
          "X-API-Key": "secret",
        },
      },
    );

    expect(captured[0].authorization).toBeUndefined();
    expect(captured[0].apiKey).toBeUndefined();
    expect(captured[0].data).toEqual({
      prompt: "雨夜街道",
      parameters: { duration: 5 },
    });
  });

  it("管理员配置保留路由字段但仍移除明文凭据", async () => {
    const { apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];

    await apiClient.post(
      "/admin/configuration/versions",
      {
        provider: "dashscope",
        provider_model_id: "wan-image-v1",
        secret_ref: "env://DASHSCOPE_API_KEY",
        api_key: "must-not-leave-browser",
      },
      { adapter: captureAdapter(captured) },
    );

    expect(captured[0].data).toEqual({
      provider: "dashscope",
      provider_model_id: "wan-image-v1",
      secret_ref: "env://DASHSCOPE_API_KEY",
    });
  });

  it("401 响应投影为中文错误并广播会话失效", async () => {
    const { SESSION_EXPIRED_EVENT, apiClient, getSafeApiError } = await import("@/lib/api");
    const listener = vi.fn();
    window.addEventListener(SESSION_EXPIRED_EVENT, listener);

    const adapter = async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
      const response: AxiosResponse = {
        config,
        data: { code: "SESSION_EXPIRED", message: "raw server detail" },
        headers: new AxiosHeaders({ "x-correlation-id": "request-100" }),
        status: 401,
        statusText: "Unauthorized",
      };
      throw new AxiosError(
        "Request failed",
        "ERR_BAD_REQUEST",
        config,
        undefined,
        response,
      );
    };

    const error = await apiClient.get("/projects", { adapter }).catch((reason) => reason);
    const safe = getSafeApiError(error);

    expect(safe).toEqual({
      code: "SESSION_EXPIRED",
      message: "登录状态已失效，请重新登录",
      status: 401,
      correlationId: "request-100",
    });
    expect(listener).toHaveBeenCalledTimes(1);
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual(safe);
    window.removeEventListener(SESSION_EXPIRED_EVENT, listener);
  });

  it("fetch 兼容路径同样携带凭据、CSRF 并剥离覆盖字段", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    const { apiFetch } = await import("@/lib/api");

    await apiFetch("/voice/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "台词", provider: "browser-provider", apiKey: "secret" }),
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(init.credentials).toBe("include");
    expect(headers.get("X-CSRF-Token")).toBe("csrf-test");
    expect(JSON.parse(String(init.body))).toEqual({ text: "台词" });
  });

  it("Playground 云端生成仅提交媒体 ID、幂等键和服务端允许参数", async () => {
    const { apiClient, playgroundApi, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const data = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
      captured.push({
        url: config.url,
        workspaceId: config.headers.get("X-Workspace-ID") as string | undefined,
        idempotencyKey: config.headers.get("Idempotency-Key") as string | undefined,
        data,
      });
      return {
        config,
        data: {
          task_id: "task-1",
          status: "queued",
          quoted_microtickets: "250000",
          quoted_tickets: "0.25",
          tokens_per_ticket: "1000",
          actual_model: {
            display_name: "平台图像模型",
            model_id: "provider-image-v1",
          },
        },
        headers: new AxiosHeaders(),
        status: 202,
        statusText: "Accepted",
      };
    };

    const mediaId = "107";
    const result = await playgroundApi.generate({
      mode: "i2i",
      model_id: "browser-model",
      prompt: "雨夜街道",
      input_media: [`media:${mediaId}`],
      parameters: { size: "1024x1024" },
    });

    expect(captured[0]).toMatchObject({
      workspaceId: "workspace-1",
      data: {
        mode: "i2i",
        prompt: "雨夜街道",
        media_ids: [mediaId],
        parameters: { size: "1024x1024" },
      },
    });
    expect(captured[0].data).not.toHaveProperty("model_id");
    expect(captured[0].data).not.toHaveProperty("input_media");
    expect((captured[0].data as Record<string, unknown>).idempotency_key).toMatch(/^playground:/);
    expect(captured[0].idempotencyKey).toBe(
      (captured[0].data as Record<string, unknown>).idempotency_key,
    );
    expect(result).toMatchObject({
      id: "task-1",
      status: "pending",
      actual_model_name: "平台图像模型",
      actual_model_id: "provider-image-v1",
      input_media: [`media:${mediaId}`],
      quoted_tickets: "0.25",
    });
  });

  it("PC 文生图固定为 Xlinks 单图契约且不允许浏览器覆盖路由", async () => {
    const { api, apiClient, playgroundApi, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const data = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
      captured.push({ url: config.url, data });
      return {
        config,
        data: config.url?.endsWith("/playground/generate")
          ? {
              task_id: "task-t2i-1",
              status: "queued",
              quoted_microtickets: "1000",
              quoted_tickets: "1",
              tokens_per_ticket: "1000",
              actual_model: {
                display_name: "GPT Image 2 · Xlinks",
                model_id: "gpt-image-2",
              },
            }
          : { ok: true },
        headers: new AxiosHeaders(),
        status: 202,
        statusText: "Accepted",
      };
    };

    await api.generateAsset(
      "5",
      "character-1",
      "character",
      "3D国漫都市风格",
      "",
      "reference_sheet",
      "普通男性角色设定图",
      true,
      "文字、水印",
      4,
      "browser-model",
      "9:16",
    );
    await playgroundApi.generate({
      mode: "t2i",
      model_id: "browser-model",
      prompt: "现代都市角色",
      input_media: [],
      parameters: {
        size: "16:9",
        quality: "unsupported",
        background: "transparent",
      },
      batch_size: 4,
    });

    expect(captured[0]).toMatchObject({
      url: expect.stringContaining("/projects/5/assets/generate"),
      data: {
        asset_id: "character-1",
        asset_type: "character",
        parameters: {
          count: 1,
          size: "1024x1536",
          quality: "high",
          output_format: "png",
          background: "auto",
        },
      },
    });
    expect(captured[0].data).not.toHaveProperty("model_name");
    expect(captured[0].data).not.toHaveProperty("provider");
    expect(captured[1]).toMatchObject({
      url: expect.stringContaining("/playground/generate"),
      data: {
        mode: "t2i",
        prompt: "现代都市角色",
        parameters: {
          count: 1,
          size: "1536x1024",
          quality: "high",
          output_format: "png",
          background: "transparent",
        },
        batch_size: 1,
      },
    });
    expect(captured[1].data).not.toHaveProperty("model_id");
    expect(captured[1].data).not.toHaveProperty("provider");
    expect(captured[1].data).not.toHaveProperty("media_ids");
  });

  it("Playground 模板写入携带乐观版本", async () => {
    const { apiClient, playgroundApi } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    apiClient.defaults.adapter = captureAdapter(captured);

    await playgroundApi.updateTemplate("template-1", 3, { name: "电影分镜" });
    await playgroundApi.deleteTemplate("template-1", 4);

    expect(captured.map((request) => request.ifMatch)).toEqual(["3", "4"]);
  });

  it("其他云端 AI 路由自动携带幂等键并把媒体引用转换为媒体 ID", async () => {
    const { apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    const mediaId = "107";

    await apiClient.post(
      "/projects/project-1/video_tasks",
      {
        prompt: "镜头推进",
        image_url: `media:${mediaId}`,
        reference_video_urls: [`media:${mediaId}`],
        duration: 5,
      },
      { adapter: captureAdapter(captured) },
    );

    expect(captured[0].idempotencyKey).toMatch(/^ai:/);
    expect(captured[0].data).toEqual({
      prompt: "镜头推进",
      duration: 5,
      media_ids: [mediaId],
    });
  });

  it("剧本分析使用长任务超时，不继承全局六十秒限制", async () => {
    const { api, apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    apiClient.defaults.adapter = captureAdapter(captured);

    await api.extractPreview("project-1", "第一幕：矿区停电。");

    expect(captured[0]).toMatchObject({
      url: expect.stringContaining("/projects/project-1/extract_preview"),
      timeout: 300_000,
      data: { text: "第一幕：矿区停电。" },
    });
  });

  it("风格分析使用长任务超时，不继承全局六十秒限制", async () => {
    const { api, apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    apiClient.defaults.adapter = captureAdapter(captured);

    await api.analyzeScriptForStyles("project-1", "第一幕：矿区停电。");

    expect(captured[0]).toMatchObject({
      url: expect.stringContaining("/projects/project-1/art_direction/analyze"),
      timeout: 300_000,
      data: { script_text: "第一幕：矿区停电。" },
    });
  });

  it("确认实体提取时提交完整预览结果", async () => {
    const { api, apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    apiClient.defaults.adapter = captureAdapter(captured);
    const extraction = {
      characters: [{ id: "character-1", name: "沈砚", description: "黑色工装" }],
      scenes: [{ id: "scene-1", name: "昆仑轨道港", description: "冷白色大厅" }],
      props: [{ id: "prop-1", name: "黑色主螺栓", description: "磨损的关键零件" }],
    };

    await api.applyExtraction("project-1", "第一幕：矿区停电。", extraction);

    expect(captured[0]).toMatchObject({
      url: expect.stringContaining("/projects/project-1/extraction"),
      data: {
        text: "第一幕：矿区停电。",
        ...extraction,
      },
    });
  });

  it("视频任务只提交媒体 ID，不泄露兼容层 URL 字段", async () => {
    const { api, apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    const mediaId = "107";
    apiClient.defaults.adapter = captureAdapter(captured);

    await api.createVideoTask(
      "project-1",
      `media:${mediaId}`,
      "镜头推进",
      3,
      undefined,
      "720p",
      true,
      "",
      true,
      "",
      1,
      "happyhorse-i2v",
      "frame-1",
      "multi",
      "i2v",
      [],
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      [],
      "16:9",
      "t2i_i2v",
    );

    expect(captured[0].data).toMatchObject({
      prompt: "镜头推进",
      duration: 3,
      resolution: "720p",
      frame_id: "frame-1",
      generation_mode: "i2v",
      media_ids: [mediaId],
      parameters: {
        model_choice: "happyhorse-i2v",
        duration: 3,
        resolution: "720p",
        ratio: "16:9",
        output_count: 1,
      },
    });
    expect(captured[0].data).not.toHaveProperty("image_url");
    expect(captured[0].data).not.toHaveProperty("audio_url");
    expect(captured[0].data).not.toHaveProperty("reference_video_urls");
    expect(captured[0].data).not.toHaveProperty("reference_image_urls");
  });

  it("Grok 视频只提交其 Xlinks 参数契约", async () => {
    const { api, apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    apiClient.defaults.adapter = captureAdapter(captured);

    await api.createVideoTask(
      "project-1",
      "media:107",
      "镜头推进",
      5,
      42,
      "720p",
      true,
      "",
      true,
      "反向模糊",
      1,
      "grok-imagine-video",
      "frame-1",
      "single",
      "i2v",
      [],
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      [],
      "16:9",
      "t2i_i2v",
    );

    const data = captured[0].data as { parameters: Record<string, unknown> };
    expect(data.parameters).toEqual({
      model_choice: "grok-imagine-video",
      duration: 5,
      resolution: "720p",
      output_count: 1,
      negative_prompt: "反向模糊",
      seed: 42,
    });
    expect(data.parameters).not.toHaveProperty("ratio");
    expect(data.parameters).not.toHaveProperty("prompt_extend");
    expect(data.parameters).not.toHaveProperty("audio");
  });

  it("Seedance I2V 只提交当前云端服务端白名单参数", async () => {
    const { api, apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    apiClient.defaults.adapter = captureAdapter(captured);

    await api.createVideoTask(
      "project-1",
      "media:107",
      "镜头推进",
      5,
      undefined,
      "1080p",
      false,
      "",
      true,
      "",
      1,
      "seedance-2.0-i2v",
      "frame-1",
      "single",
      "i2v",
      [],
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      [],
      "16:9",
      "t2i_i2v",
      true,
    );

    const data = captured[0].data as { parameters: Record<string, unknown> };
    expect(data.parameters).toEqual({
      model_choice: "seedance-2.0-i2v",
      duration: 5,
      resolution: "1080p",
      output_count: 1,
    });
  });

  it("并发分镜写入遇到版本冲突后刷新版本并自动重试", async () => {
    const { api, apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const patchVersions: Array<string | undefined> = [];
    let patchAttempts = 0;
    setActiveWorkspaceId("workspace-1");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const method = String(config.method).toLowerCase();
      if (method === "patch") {
        patchAttempts += 1;
        patchVersions.push(config.headers.get("If-Match") as string | undefined);
        if (patchAttempts === 1) {
          const response: AxiosResponse = {
            config,
            data: { code: "CONTENT_VERSION_CONFLICT", message: "内容版本已变化" },
            headers: new AxiosHeaders(),
            status: 409,
            statusText: "Conflict",
          };
          throw new AxiosError("Request failed", "ERR_BAD_REQUEST", config, undefined, response);
        }
        return {
          config,
          data: { id: "frame-1", version: 5, t2i_image_urls: ["media:107"] },
          headers: new AxiosHeaders(),
          status: 200,
          statusText: "OK",
        };
      }
      return {
        config,
        data: { id: "project-1", version: patchAttempts === 0 ? 3 : 4 },
        headers: new AxiosHeaders(),
        status: 200,
        statusText: "OK",
      };
    };

    await apiClient.get("/projects/project-1");
    const frame = await api.updateFrameWorkbench("project-1", "frame-1", {
      t2i_image_urls: ["media:107"],
      t2i_selected_index: 0,
    });

    expect(patchAttempts).toBe(2);
    expect(patchVersions).toEqual(["3", "4"]);
    expect(frame).toMatchObject({ id: "frame-1", version: 5 });
  });

  it("内容与资产写入自动使用当前工作区缓存的乐观版本", async () => {
    const { apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");

    await apiClient.get("/projects/project-1", {
      adapter: async (config) => ({
        config,
        data: { id: "project-1", title: "测试项目", version: 4 },
        headers: new AxiosHeaders(),
        status: 200,
        statusText: "OK",
      }),
    });
    await apiClient.get("/projects/project-1/assets", {
      adapter: async (config) => ({
        config,
        data: {
          characters: [{
            id: "character-1",
            asset_record_id: "record-1",
            version: 7,
          }],
          scenes: [],
          props: [],
        },
        headers: new AxiosHeaders(),
        status: 200,
        statusText: "OK",
      }),
    });

    await apiClient.put("/projects/project-1/text", { text: "新剧本" }, {
      adapter: captureAdapter(captured),
    });
    await apiClient.post("/projects/project-1/assets/toggle_lock", {
      asset_id: "character-1",
      asset_type: "character",
    }, { adapter: captureAdapter(captured) });

    expect(captured.map((request) => request.ifMatch)).toEqual(["4", "7"]);
  });

  it("云端任务轮询使用持久任务端点并把媒体 ID 投影为媒体引用", async () => {
    const { api, apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const requestedUrls: string[] = [];
    const mediaId = "107";
    setActiveWorkspaceId("workspace-1");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      requestedUrls.push(String(config.url));
      const data = String(config.url).includes(`/media/${mediaId}/access`)
        ? {
            media_id: mediaId,
            url: "https://private.example/signed-video.mp4",
            expires_at: new Date(Date.now() + 300_000).toISOString(),
          }
        : {
            id: "task-1",
            workspace_id: "workspace-1",
            project_id: "project-1",
            capability: "video.i2v",
            status: "succeeded",
            status_zh: "已完成",
            quoted_microtickets: "250000",
            quoted_tickets: "0.25",
            cancellation_requested: false,
            support_review: false,
            safe_error: null,
            media_ids: [mediaId],
          };
      return {
        config,
        data,
        headers: new AxiosHeaders(),
        status: 200,
        statusText: "OK",
      };
    };

    const task = await api.getVideoTaskStatus("project-1", "task-1");

    expect(requestedUrls[0]).toContain("/ai/tasks/task-1/status");
    expect(requestedUrls.some((url) => url.includes(`/media/${mediaId}/access`))).toBe(true);
    expect(task).toMatchObject({
      id: "task-1",
      status: "completed",
      raw_status: "succeeded",
      video_url: `media:${mediaId}`,
      media_references: [`media:${mediaId}`],
    });
  });

  it("R2V 提示词润色等待持久任务并解析双语 JSON", async () => {
    const { api, apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const data = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
      captured.push({
        url: config.url,
        workspaceId: config.headers.get("X-Workspace-ID") as string | undefined,
        idempotencyKey: config.headers.get("Idempotency-Key") as string | undefined,
        data,
      });
      const isStatus = String(config.url).includes("/ai/tasks/task-polish-1/status");
      return {
        config,
        data: isStatus
          ? {
              id: "task-polish-1",
              workspace_id: "workspace-1",
              project_id: "5",
              capability: "prompt.polish",
              status: "succeeded",
              status_zh: "已完成",
              quoted_microtickets: "40960000",
              quoted_tickets: "40.96",
              cancellation_requested: false,
              support_review: false,
              safe_error: null,
              media_ids: [],
              result_content: "```json\n{\"prompt_cn\":\"缓慢推近张成\",\"prompt_en\":\"Slowly push in on Zhang Cheng\"}\n```",
            }
          : {
              task_id: "task-polish-1",
              status: "queued",
              capability: "prompt.polish",
            },
        headers: new AxiosHeaders(),
        status: isStatus ? 200 : 202,
        statusText: isStatus ? "OK" : "Accepted",
      };
    };

    const result = await api.polishR2VPrompt(
      "[character1:张成]望向酒店窗口",
      [{ description: "张成：疲惫的普通男性" }],
      "",
      "5",
      "",
      ["media:246"],
    );

    expect(result).toEqual({
      prompt_cn: "缓慢推近张成",
      prompt_en: "Slowly push in on Zhang Cheng",
    });
    expect(captured).toHaveLength(2);
    expect(captured[0]).toMatchObject({
      workspaceId: "workspace-1",
      data: {
        draft_prompt: "[character1:张成]望向酒店窗口",
        slots: [{ description: "张成：疲惫的普通男性" }],
        feedback: "",
        script_id: "5",
        prev_cn: "",
        media_ids: ["246"],
      },
    });
    expect(captured[0].idempotencyKey).toMatch(/^ai:/);
    expect(captured[1].url).toContain("/ai/tasks/task-polish-1/status");
  });

  it("批量对白生成等待持久语音任务并返回最终统计", async () => {
    const { api, apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          task_id: "task-dialogue-1",
          status: "queued",
          capability: "speech.tts",
        }),
        {
          status: 202,
          headers: { "content-type": "application/json" },
        },
      ),
    );
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      captured.push({
        url: config.url,
        workspaceId: config.headers.get("X-Workspace-ID") as string | undefined,
        data: config.data,
      });
      return {
        config,
        data: {
          id: "task-dialogue-1",
          workspace_id: "workspace-1",
          project_id: "5",
          capability: "speech.tts",
          status: "succeeded",
          status_zh: "已完成",
          quoted_microtickets: "8000",
          quoted_tickets: "0.008",
          cancellation_requested: false,
          support_review: false,
          safe_error: null,
          media_ids: ["301", "302"],
          result_content: {
            operation: "audio.dialogue.batch",
            _batch_stats: {
              generated: 2,
              skipped: 1,
              failed: 0,
              no_voice: 0,
              total: 3,
            },
          },
        },
        headers: new AxiosHeaders(),
        status: 200,
        statusText: "OK",
      };
    };

    const result = await api.generateDialogueAudioBatch("5");

    expect(result._batch_stats).toEqual({
      generated: 2,
      skipped: 1,
      failed: 0,
      no_voice: 0,
      total: 3,
    });
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/projects/5/dialogue_audio/batch"),
      expect.objectContaining({ method: "POST" }),
    );
    expect(captured).toHaveLength(3);
    expect(captured.every((request) => request.workspaceId === "workspace-1")).toBe(true);
    expect(captured[0].url).toContain("/ai/tasks/task-dialogue-1/status");
    expect(captured.slice(1).map((request) => request.url)).toEqual([
      expect.stringContaining("/media/301/access"),
      expect.stringContaining("/media/302/access"),
    ]);
  });

  it("下一集钩子通过持久文本任务生成并按项目版本回写", async () => {
    const { api, apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const data = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
      captured.push({
        url: config.url,
        csrf: config.headers.get("X-CSRF-Token") as string | undefined,
        workspaceId: config.headers.get("X-Workspace-ID") as string | undefined,
        ifMatch: config.headers.get("If-Match") as string | undefined,
        idempotencyKey: config.headers.get("Idempotency-Key") as string | undefined,
        data,
      });
      const url = String(config.url);
      const method = String(config.method).toLowerCase();
      const responseData = url.includes("/ai/tasks/task-hook-1/status")
        ? {
            id: "task-hook-1",
            workspace_id: "workspace-1",
            project_id: "project-1",
            capability: "prompt.polish",
            status: "succeeded",
            status_zh: "已完成",
            quoted_microtickets: "120000",
            quoted_tickets: "0.12",
            cancellation_requested: false,
            support_review: false,
            safe_error: null,
            media_ids: [],
            result_content: "下一集从雨夜追逐开始。",
          }
        : method === "post"
          ? { task_id: "task-hook-1", status: "queued", version: 7 }
          : { hook: "下一集从雨夜追逐开始。", stale: false, version: 8 };
      return {
        config,
        data: responseData,
        headers: new AxiosHeaders(),
        status: method === "post" ? 202 : 200,
        statusText: "OK",
      };
    };

    const result = await api.generateNextEpisodeHook("project-1");

    expect(result).toEqual({
      hook: "下一集从雨夜追逐开始。",
      stale: false,
      version: 8,
    });
    expect(captured.map((request) => new URL(String(request.url)).pathname)).toEqual([
      "/api/v1/projects/project-1/next_hook",
      "/api/v1/ai/tasks/task-hook-1/status",
      "/api/v1/projects/project-1/next_hook",
    ]);
    expect(captured[0]).toMatchObject({
      csrf: "csrf-test",
      workspaceId: "workspace-1",
      data: undefined,
    });
    expect(captured[0].idempotencyKey).toMatch(/^next-hook:/);
    expect(captured[2]).toMatchObject({
      csrf: "csrf-test",
      workspaceId: "workspace-1",
      ifMatch: "7",
      data: { hook: "下一集从雨夜追逐开始。" },
    });
  });

  it("上一集摘要使用持久文本任务并按当前集版本回写", async () => {
    const { api, apiClient, setActiveWorkspaceId } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    setActiveWorkspaceId("workspace-1");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const data = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
      captured.push({
        url: config.url,
        csrf: config.headers.get("X-CSRF-Token") as string | undefined,
        workspaceId: config.headers.get("X-Workspace-ID") as string | undefined,
        ifMatch: config.headers.get("If-Match") as string | undefined,
        idempotencyKey: config.headers.get("Idempotency-Key") as string | undefined,
        data,
      });
      const url = String(config.url);
      const method = String(config.method).toLowerCase();
      const responseData = url.includes("/ai/tasks/task-summary-1/status")
        ? {
            id: "task-summary-1",
            workspace_id: "workspace-1",
            project_id: "project-2",
            capability: "prompt.polish",
            status: "succeeded",
            status_zh: "已完成",
            quoted_microtickets: "150000",
            quoted_tickets: "0.15",
            cancellation_requested: false,
            support_review: false,
            safe_error: null,
            media_ids: [],
            result_content: "上一集主角发现门后隐藏着关键证据。",
          }
        : method === "post"
          ? {
              task_id: "task-summary-1",
              status: "queued",
              version: 5,
              previous_episode_version: 3,
            }
          : {
              ai_summary: "上一集主角发现门后隐藏着关键证据。",
              ai_summary_stale: false,
              previous_episode_id: "project-1",
              previous_episode_title: "第一集",
              version: 6,
            };
      return {
        config,
        data: responseData,
        headers: new AxiosHeaders(),
        status: method === "post" ? 202 : 200,
        statusText: "OK",
      };
    };

    const result = await api.generatePreviousEpisodeSummary("project-2");

    expect(result).toMatchObject({
      ai_summary: "上一集主角发现门后隐藏着关键证据。",
      previous_episode_id: "project-1",
      version: 6,
    });
    expect(captured.map((request) => new URL(String(request.url)).pathname)).toEqual([
      "/api/v1/projects/project-2/previous_episode/summary",
      "/api/v1/ai/tasks/task-summary-1/status",
      "/api/v1/projects/project-2/last_episode_summary",
    ]);
    expect(captured[0].idempotencyKey).toMatch(/^previous-summary:/);
    expect(captured[2]).toMatchObject({
      ifMatch: "5",
      data: {
        ai_summary: "上一集主角发现门后隐藏着关键证据。",
        source_previous_version: 3,
      },
    });
  });

  it("平台管理查询与安全动作使用受保护的服务端端点", async () => {
    const { adminPlatformApi, apiClient } = await import("@/lib/api");
    const captured: CapturedRequest[] = [];
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const data = typeof config.data === "string" ? JSON.parse(config.data) : config.data;
      captured.push({
        url: config.url,
        csrf: config.headers.get("X-CSRF-Token") as string | undefined,
        data,
      });
      return {
        config,
        data: String(config.url).endsWith("/reset-credentials")
          ? { credential: "one-time", expires_at: "2026-08-13T10:00:00Z" }
          : String(config.url).includes("/admin/users/")
            ? { message: "操作成功" }
            : { items: [], total: 0, offset: 0, limit: 30 },
        headers: new AxiosHeaders(),
        status: 200,
        statusText: "OK",
      };
    };

    await adminPlatformApi.listUsers({ query: "+86138" });
    await adminPlatformApi.createUser(
      "+8613800138088",
      "secure-pass-2026",
      "运营后台开户",
    );
    await adminPlatformApi.listTasks({ status: "running" });
    await adminPlatformApi.listUsage({ outcome: "succeeded" });
    await adminPlatformApi.listAuditEvents({ action: "ticket" });
    await adminPlatformApi.listImportBatches({ status: "dry_run" });
    await adminPlatformApi.updateUserStatus("user-1", "suspended", "风险处置");
    await adminPlatformApi.revokeUserSessions("user-1", "账号安全处置");
    await adminPlatformApi.issueResetCredential("user-1", "人工核验通过");

    expect(captured.map((request) => new URL(String(request.url)).pathname)).toEqual([
      "/api/v1/admin/users",
      "/api/v1/admin/users",
      "/api/v1/admin/tasks",
      "/api/v1/admin/usage",
      "/api/v1/admin/audit-events",
      "/api/v1/admin/import-batches",
      "/api/v1/admin/users/user-1/suspend",
      "/api/v1/admin/users/user-1/revoke-sessions",
      "/api/v1/admin/users/user-1/reset-credentials",
    ]);
    const mutationRequests = [captured[1], ...captured.slice(6)];
    expect(mutationRequests.every((request) => request.csrf === "csrf-test")).toBe(true);
    expect(mutationRequests.map((request) => request.data)).toEqual([
      {
        phone: "+8613800138088",
        password: "secure-pass-2026",
        reason: "运营后台开户",
      },
      { reason: "风险处置" },
      { reason: "账号安全处置" },
      { reason: "人工核验通过" },
    ]);
  });

  it("媒体授权失败统一投影为不泄露归属的中文错误", async () => {
    const { apiClient, getSafeApiError, resolveMediaUrl, setActiveWorkspaceId } = await import("@/lib/api");
    const mediaId = "107";
    setActiveWorkspaceId("workspace-media-denied");
    apiClient.defaults.adapter = async (config): Promise<AxiosResponse> => {
      const response: AxiosResponse = {
        config,
        data: { code: "MEDIA_NOT_FOUND", message: "raw ownership detail" },
        headers: new AxiosHeaders({ "x-correlation-id": "media-denied-1" }),
        status: 404,
        statusText: "Not Found",
      };
      throw new AxiosError("Request failed", "ERR_BAD_REQUEST", config, undefined, response);
    };

    const error = await resolveMediaUrl(`media:${mediaId}`).catch((reason) => reason);

    expect(getSafeApiError(error)).toEqual({
      code: "MEDIA_NOT_FOUND",
      message: "媒体不存在或无权访问",
      status: 404,
      correlationId: "media-denied-1",
    });
  });
});
