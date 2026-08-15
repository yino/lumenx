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

    const mediaId = "07f802fc-69d9-4cc8-b62e-0f039b770aac";
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
    const mediaId = "07f802fc-69d9-4cc8-b62e-0f039b770aac";

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
    const mediaId = "07f802fc-69d9-4cc8-b62e-0f039b770aac";
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
          : String(config.url).includes("/auth/admin/")
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
      "/api/v1/auth/admin/users/user-1/suspend",
      "/api/v1/auth/admin/users/user-1/revoke-sessions",
      "/api/v1/auth/admin/users/user-1/reset-credentials",
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
    const mediaId = "07f802fc-69d9-4cc8-b62e-0f039b770aac";
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
