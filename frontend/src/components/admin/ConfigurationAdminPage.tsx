"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  CheckCircle2,
  CircleOff,
  Clock3,
  CopyPlus,
  Database,
  Loader2,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ServerCog,
  Trash2,
  Zap,
} from "lucide-react";
import {
  adminConfigurationApi,
  type AdminAICapability,
  type AdminConfigurationDraft,
  type AdminConfigurationVersion,
  type AdminModelRoute,
  type AdminPlatformConfig,
} from "@/lib/api";

const CAPABILITIES: Array<{ id: AdminAICapability; label: string }> = [
  { id: "script.analysis", label: "剧本分析" },
  { id: "prompt.polish", label: "提示词润色" },
  { id: "image.t2i", label: "文生图" },
  { id: "image.i2i", label: "图生图" },
  { id: "video.t2v", label: "文生视频" },
  { id: "video.i2v", label: "图生视频" },
  { id: "video.r2v", label: "参考生视频" },
  { id: "video.v2v", label: "视频编辑" },
  { id: "speech.tts", label: "语音合成" },
  { id: "audio.sfx", label: "音效生成" },
];

const STATUS_COPY: Record<AdminConfigurationVersion["status"], string> = {
  draft: "草稿",
  active: "生效中",
  superseded: "已替代",
  disabled: "已停用",
};

const STATUS_CLASS: Record<AdminConfigurationVersion["status"], string> = {
  draft: "border-amber-400/30 bg-amber-400/10 text-amber-300",
  active: "border-emerald-400/30 bg-emerald-400/10 text-emerald-300",
  superseded: "border-sky-400/30 bg-sky-400/10 text-sky-300",
  disabled: "border-border-subtle bg-glass text-text-muted",
};

const DEFAULT_PLATFORM: AdminPlatformConfig = {
  tokens_per_ticket: 1000,
  registration_initial_grant_microtickets: 0,
  session_idle_seconds: 604800,
  session_absolute_seconds: 2592000,
  max_sessions_per_user: 5,
  max_ai_concurrency_per_user: 2,
  exposed_capabilities: ["image.t2i"],
  feature_flags: {
    registration_mode: "disabled",
    new_ai_tasks_enabled: false,
  },
  operational: {
    signed_media_url_seconds: 300,
    soft_delete_retention_days: 30,
    stale_hold_minutes: 30,
  },
};

interface EditableRoute extends Omit<
  AdminModelRoute,
  "default_parameters" | "parameter_schema" | "metering_formula" | "fallback_policy"
> {
  defaultParametersText: string;
  parameterSchemaText: string;
  meteringFormulaText: string;
  fallbackPolicyText: string;
}

interface EditableDraft {
  reason: string;
  platform: AdminPlatformConfig;
  routes: EditableRoute[];
}

function capabilityLabel(capability: AdminAICapability): string {
  return CAPABILITIES.find((item) => item.id === capability)?.label ?? capability;
}

function registrationModeLabel(mode: AdminPlatformConfig["feature_flags"]["registration_mode"]): string {
  if (mode === "disabled") return "已关闭";
  if (mode === "invite_only") return "仅限邀请";
  if (mode === "open") return "直接开放";
  return "验证码开放";
}

function formatDate(value?: string | null): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function errorMessage(error: unknown): string {
  const response = (error as {
    response?: {
      status?: number;
      data?: { message?: string; detail?: string | { message?: string } };
    };
  })?.response;
  const detail = response?.data?.detail;
  if (response?.data?.message) return response.data.message;
  if (response?.status === 404 && detail === "Not Found") {
    return "配置管理服务仅在云端模式可用";
  }
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  if (error instanceof Error) return error.message;
  return "操作失败，请稍后重试";
}

function toEditableRoute(route: AdminModelRoute): EditableRoute {
  return {
    capability: route.capability,
    display_name_zh: route.display_name_zh,
    provider: route.provider,
    provider_model_id: route.provider_model_id,
    enabled: route.enabled,
    is_primary: route.is_primary,
    priority: route.priority,
    secret_ref: route.secret_ref,
    defaultParametersText: JSON.stringify(route.default_parameters, null, 2),
    parameterSchemaText: JSON.stringify(route.parameter_schema, null, 2),
    meteringFormulaText: JSON.stringify(route.metering_formula, null, 2),
    fallbackPolicyText: JSON.stringify(route.fallback_policy, null, 2),
  };
}

function newEditableRoute(): EditableRoute {
  return toEditableRoute({
    capability: "image.t2i",
    display_name_zh: "新图像模型",
    provider: "dashscope",
    provider_model_id: "",
    enabled: false,
    is_primary: false,
    priority: 100,
    default_parameters: {},
    parameter_schema: [],
    metering_formula: {
      kind: "image",
      base_tokens: 0,
      per_image_tokens: 100,
      max_images: 1,
      resolution_multipliers: { "1024x1024": 1 },
    },
    fallback_policy: {
      enabled: false,
      eligible_error_codes: [],
      max_attempts: 1,
      require_nonbillable_previous_attempt: true,
    },
    secret_ref: "DASHSCOPE_API_KEY",
  });
}

function toEditableDraft(source?: AdminConfigurationVersion | null): EditableDraft {
  return {
    reason: "",
    platform: source
      ? structuredClone(source.platform)
      : structuredClone(DEFAULT_PLATFORM),
    routes: source?.routes.map(toEditableRoute) ?? [newEditableRoute()],
  };
}

function parseJson<T>(value: string, label: string): T {
  try {
    return JSON.parse(value) as T;
  } catch {
    throw new Error(`${label}不是有效的 JSON`);
  }
}

function toConfigurationDraft(draft: EditableDraft): AdminConfigurationDraft {
  return {
    reason: draft.reason.trim(),
    platform: draft.platform,
    routes: draft.routes.map((route) => ({
      capability: route.capability,
      display_name_zh: route.display_name_zh.trim(),
      provider: route.provider.trim(),
      provider_model_id: route.provider_model_id.trim(),
      enabled: route.enabled,
      is_primary: route.is_primary,
      priority: route.priority,
      secret_ref: route.secret_ref.trim(),
      default_parameters: parseJson(route.defaultParametersText, "默认参数"),
      parameter_schema: parseJson(route.parameterSchemaText, "参数规则"),
      metering_formula: parseJson(route.meteringFormulaText, "计量公式"),
      fallback_policy: parseJson(route.fallbackPolicyText, "回退规则"),
    })),
  };
}

function StatusBadge({ status }: { status: AdminConfigurationVersion["status"] }) {
  return (
    <span className={`inline-flex h-6 items-center rounded border px-2 text-xs font-medium ${STATUS_CLASS[status]}`}>
      {STATUS_COPY[status]}
    </span>
  );
}

function NumberField({
  label,
  value,
  min = 0,
  onChange,
}: {
  label: string;
  value: number;
  min?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="space-y-1.5">
      <span className="block text-xs font-medium text-text-secondary">{label}</span>
      <input
        type="number"
        min={min}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 font-mono text-sm text-foreground outline-none transition-colors focus:border-primary/60"
      />
    </label>
  );
}

function JsonField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="space-y-1.5">
      <span className="block text-xs font-medium text-text-secondary">{label}</span>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        className="min-h-32 w-full resize-y rounded-md border border-glass-border bg-glass px-3 py-2 font-mono text-xs leading-5 text-foreground outline-none transition-colors focus:border-primary/60"
      />
    </label>
  );
}

export default function ConfigurationAdminPage() {
  const [versions, setVersions] = useState<AdminConfigurationVersion[]>([]);
  const [activeVersion, setActiveVersion] = useState<AdminConfigurationVersion | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditableDraft | null>(null);
  const [actionReason, setActionReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [workerConcurrency, setWorkerConcurrency] = useState<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const selected = useMemo(
    () => versions.find((version) => version.id === selectedId) ?? versions[0] ?? null,
    [selectedId, versions],
  );

  const load = useCallback(async (preferredId?: string) => {
    setLoading(true);
    setNotice(null);
    try {
      const loadedVersions = await adminConfigurationApi.listVersions();
      const deploymentState = await adminConfigurationApi.getDeploymentState();
      setWorkerConcurrency(deploymentState.worker_concurrency);
      let loadedActive: AdminConfigurationVersion | null = null;
      try {
        loadedActive = await adminConfigurationApi.getActive();
      } catch (error) {
        const status = (error as { response?: { status?: number } })?.response?.status;
        if (status !== 404) throw error;
      }
      setVersions(loadedVersions);
      setActiveVersion(loadedActive);
      setSelectedId(preferredId ?? loadedActive?.id ?? loadedVersions[0]?.id ?? null);
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const runAction = async (
    name: string,
    action: () => Promise<AdminConfigurationVersion | { message: string }>,
    successText: string,
  ) => {
    setBusy(name);
    setNotice(null);
    try {
      const result = await action();
      const preferredId = "id" in result ? result.id : selected?.id;
      setNotice({
        kind: "success",
        text: "message" in result ? result.message : successText,
      });
      setActionReason("");
      await load(preferredId);
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  const requireActionReason = (): string | null => {
    const reason = actionReason.trim();
    if (!reason) {
      setNotice({ kind: "error", text: "请填写本次变更原因" });
      return null;
    }
    return reason;
  };

  const saveDraft = async () => {
    if (!editor) return;
    if (!editor.reason.trim()) {
      setNotice({ kind: "error", text: "请填写创建草稿的原因" });
      return;
    }
    setBusy("save");
    setNotice(null);
    try {
      const created = await adminConfigurationApi.createVersion(toConfigurationDraft(editor));
      setEditor(null);
      setNotice({ kind: "success", text: `配置版本 V${created.version_number} 已保存为草稿` });
      await load(created.id);
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  const updatePlatform = <K extends keyof AdminPlatformConfig>(
    key: K,
    value: AdminPlatformConfig[K],
  ) => {
    setEditor((current) => current && ({
      ...current,
      platform: { ...current.platform, [key]: value },
    }));
  };

  const updateOperational = (
    key: keyof AdminPlatformConfig["operational"],
    value: number,
  ) => {
    setEditor((current) => current && ({
      ...current,
      platform: {
        ...current.platform,
        operational: { ...current.platform.operational, [key]: value },
      },
    }));
  };

  const updateFeatureFlag = (
    key: "new_ai_tasks_enabled",
    value: boolean,
  ) => {
    setEditor((current) => current && ({
      ...current,
      platform: {
        ...current.platform,
        feature_flags: { ...current.platform.feature_flags, [key]: value },
      },
    }));
  };

  const updateRoute = (index: number, patch: Partial<EditableRoute>) => {
    setEditor((current) => current && ({
      ...current,
      routes: current.routes.map((route, routeIndex) =>
        routeIndex === index ? { ...route, ...patch } : route
      ),
    }));
  };

  const toggleCapability = (capability: AdminAICapability) => {
    if (!editor) return;
    const exposed = editor.platform.exposed_capabilities;
    updatePlatform(
      "exposed_capabilities",
      exposed.includes(capability)
        ? exposed.filter((item) => item !== capability)
        : [...exposed, capability],
    );
  };

  const enabledRoutes = activeVersion?.routes.filter((route) => route.enabled).length ?? 0;

  return (
    <div className="min-h-full bg-background text-foreground">
      <header className="border-b border-glass-border px-5 py-5 md:px-8">
        <div className="mx-auto flex max-w-[1480px] flex-wrap items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-primary/30 bg-primary/10 text-primary">
              <ServerCog size={20} />
            </div>
            <div className="min-w-0">
              <h1 className="text-xl font-semibold">平台配置</h1>
              <p className="mt-0.5 text-sm text-text-secondary">模型路由、计量规则与平台限制</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              title="刷新配置"
              aria-label="刷新配置"
              onClick={() => void load(selected?.id)}
              disabled={loading}
              className="flex h-10 w-10 items-center justify-center rounded-md border border-glass-border bg-glass text-text-secondary transition-colors hover:bg-hover-bg hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw size={17} className={loading ? "animate-spin" : ""} />
            </button>
            <button
              type="button"
              onClick={() => setEditor(toEditableDraft(selected ?? activeVersion))}
              className="inline-flex h-10 items-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-on-accent transition-colors hover:bg-primary-hover"
            >
              <CopyPlus size={16} />
              新建草稿
            </button>
          </div>
        </div>
      </header>

      <section className="border-b border-glass-border px-5 md:px-8">
        <div className="mx-auto grid max-w-[1480px] grid-cols-2 divide-x divide-glass-border md:grid-cols-4">
          {[
            ["当前版本", activeVersion ? `V${activeVersion.version_number}` : "未激活"],
            ["换算比例", activeVersion ? `1 算力券 = ${activeVersion.platform.tokens_per_ticket} 计量令牌` : "-"],
            ["启用路由", String(enabledRoutes)],
            ["历史版本", String(versions.length)],
          ].map(([label, value]) => (
            <div key={label} className="px-4 py-4 first:pl-0 md:px-6">
              <p className="text-xs text-text-muted">{label}</p>
              <p className="mt-1 truncate font-mono text-sm font-semibold text-foreground">{value}</p>
            </div>
          ))}
        </div>
      </section>

      <div className="mx-auto max-w-[1480px] px-5 py-6 md:px-8">
        {notice && (
          <div
            className={`mb-5 flex items-start gap-2 rounded-md border px-3 py-2.5 text-sm ${
              notice.kind === "success"
                ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-200"
                : "border-red-400/30 bg-red-400/10 text-red-200"
            }`}
          >
            {notice.kind === "success" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
            <span>{notice.text}</span>
          </div>
        )}

        <div className="grid gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
          <aside className="self-start rounded-md border border-glass-border bg-glass backdrop-blur-xl lg:sticky lg:top-6">
            <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
              <h2 className="text-sm font-semibold">版本记录</h2>
              <span className="font-mono text-xs text-text-muted">{versions.length}</span>
            </div>
            <div className="max-h-[calc(100vh-260px)] overflow-y-auto">
              {loading ? (
                <div className="flex items-center justify-center py-10 text-text-muted">
                  <Loader2 size={18} className="animate-spin" />
                </div>
              ) : versions.length === 0 ? (
                <div className="px-4 py-8 text-center text-sm text-text-muted">暂无配置版本</div>
              ) : (
                versions.map((version) => (
                  <button
                    key={version.id}
                    type="button"
                    onClick={() => {
                      setSelectedId(version.id);
                      setEditor(null);
                      setActionReason("");
                    }}
                    className={`w-full border-b border-border-subtle px-4 py-3 text-left transition-colors last:border-b-0 ${
                      selected?.id === version.id && !editor ? "bg-primary/10" : "hover:bg-hover-bg"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-mono text-sm font-semibold">V{version.version_number}</span>
                      <StatusBadge status={version.status} />
                    </div>
                    <p className="mt-2 line-clamp-2 text-xs leading-5 text-text-secondary">{version.reason}</p>
                    <p className="mt-1.5 font-mono text-[0.625rem] text-text-muted">{formatDate(version.created_at)}</p>
                  </button>
                ))
              )}
            </div>
          </aside>

          <main className="min-w-0">
            {editor ? (
              <div className="space-y-6">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-glass-border pb-4">
                  <div>
                    <h2 className="text-lg font-semibold">新建配置草稿</h2>
                    <p className="mt-1 text-sm text-text-secondary">基于 {selected ? `V${selected.version_number}` : "平台默认值"}</p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setEditor(null)}
                      className="h-9 rounded-md border border-glass-border bg-glass px-3 text-sm text-text-secondary hover:bg-hover-bg hover:text-foreground"
                    >
                      取消
                    </button>
                    <button
                      type="button"
                      onClick={() => void saveDraft()}
                      disabled={busy === "save"}
                      className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50"
                    >
                      {busy === "save" ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
                      保存草稿
                    </button>
                  </div>
                </div>

                <section className="border-b border-glass-border pb-6">
                  <h3 className="mb-4 text-sm font-semibold">变更说明</h3>
                  <input
                    value={editor.reason}
                    onChange={(event) => setEditor({ ...editor, reason: event.target.value })}
                    placeholder="填写创建此版本的原因"
                    className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 text-sm outline-none focus:border-primary/60"
                  />
                </section>

                <section className="border-b border-glass-border pb-6">
                  <div className="mb-4 flex items-center gap-2">
                    <CircleOff size={16} className="text-primary" />
                    <h3 className="text-sm font-semibold">分阶段开放</h3>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="rounded-md border border-glass-border bg-glass px-3 py-3 text-sm">
                      <span className="mb-2 block">用户注册模式</span>
                      <select
                        value={editor.platform.feature_flags.registration_mode}
                        onChange={(event) => setEditor({
                          ...editor,
                          platform: {
                            ...editor.platform,
                            feature_flags: {
                              ...editor.platform.feature_flags,
                              registration_mode: event.target.value as AdminPlatformConfig["feature_flags"]["registration_mode"],
                            },
                          },
                        })}
                        className="h-9 w-full rounded-md border border-glass-border bg-elevated px-3 outline-none focus:border-primary/60"
                      >
                        <option value="disabled">关闭注册</option>
                        <option value="invite_only">仅限邀请</option>
                        <option value="open">直接开放注册（暂不验证手机号）</option>
                        <option value="verified_open" disabled>验证码开放注册（尚未接入）</option>
                      </select>
                    </label>
                    <label className="flex cursor-pointer items-center justify-between gap-4 rounded-md border border-glass-border bg-glass px-3 py-3 text-sm">
                      <span>允许创建 AI 新任务</span>
                      <input
                        type="checkbox"
                        checked={editor.platform.feature_flags.new_ai_tasks_enabled}
                        onChange={(event) => updateFeatureFlag("new_ai_tasks_enabled", event.target.checked)}
                        className="h-4 w-4 accent-primary"
                      />
                    </label>
                  </div>
                </section>

                <section className="border-b border-glass-border pb-6">
                  <div className="mb-4 flex items-center gap-2">
                    <Zap size={16} className="text-primary" />
                    <h3 className="text-sm font-semibold">平台限制</h3>
                  </div>
                  <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                    <NumberField label="每张算力券对应的计量令牌数" min={1} value={editor.platform.tokens_per_ticket} onChange={(value) => updatePlatform("tokens_per_ticket", value)} />
                    <NumberField label="注册赠送（微算力券）" value={editor.platform.registration_initial_grant_microtickets} onChange={(value) => updatePlatform("registration_initial_grant_microtickets", value)} />
                    <NumberField label="会话闲置时限（秒）" min={1} value={editor.platform.session_idle_seconds} onChange={(value) => updatePlatform("session_idle_seconds", value)} />
                    <NumberField label="会话绝对时限（秒）" min={1} value={editor.platform.session_absolute_seconds} onChange={(value) => updatePlatform("session_absolute_seconds", value)} />
                    <NumberField label="单用户会话上限" min={1} value={editor.platform.max_sessions_per_user} onChange={(value) => updatePlatform("max_sessions_per_user", value)} />
                    <NumberField label="单用户 AI 并发" min={1} value={editor.platform.max_ai_concurrency_per_user} onChange={(value) => updatePlatform("max_ai_concurrency_per_user", value)} />
                    <NumberField label="媒体链接有效期（秒）" min={30} value={editor.platform.operational.signed_media_url_seconds} onChange={(value) => updateOperational("signed_media_url_seconds", value)} />
                    <NumberField label="回收保留天数" min={1} value={editor.platform.operational.soft_delete_retention_days} onChange={(value) => updateOperational("soft_delete_retention_days", value)} />
                    <NumberField label="占用超时（分钟）" min={1} value={editor.platform.operational.stale_hold_minutes} onChange={(value) => updateOperational("stale_hold_minutes", value)} />
                    <div className="rounded-md border border-glass-border bg-glass px-3 py-2">
                      <span className="block text-xs text-text-muted">工作进程并发（部署管理，只读）</span>
                      <strong className="mt-1 block font-mono text-sm">{workerConcurrency ?? "-"}</strong>
                    </div>
                  </div>
                </section>

                <section className="border-b border-glass-border pb-6">
                  <h3 className="mb-3 text-sm font-semibold">开放能力</h3>
                  <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
                    {CAPABILITIES.map((capability) => {
                      const checked = editor.platform.exposed_capabilities.includes(capability.id);
                      return (
                        <label key={capability.id} className="flex cursor-pointer items-center gap-2 rounded-md border border-glass-border bg-glass px-3 py-2 text-sm">
                          <input type="checkbox" checked={checked} onChange={() => toggleCapability(capability.id)} className="accent-primary" />
                          <span>{capability.label}</span>
                        </label>
                      );
                    })}
                  </div>
                </section>

                <section className="space-y-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <Database size={16} className="text-primary" />
                      <h3 className="text-sm font-semibold">模型路由</h3>
                      <span className="font-mono text-xs text-text-muted">{editor.routes.length}</span>
                    </div>
                    <button
                      type="button"
                      onClick={() => setEditor({ ...editor, routes: [...editor.routes, newEditableRoute()] })}
                      className="inline-flex h-9 items-center gap-2 rounded-md border border-glass-border bg-glass px-3 text-sm text-text-secondary hover:bg-hover-bg hover:text-foreground"
                    >
                      <Plus size={15} />
                      添加路由
                    </button>
                  </div>

                  {editor.routes.map((route, index) => (
                    <article key={`${route.provider_model_id}-${index}`} className="rounded-md border border-glass-border bg-glass p-4">
                      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs text-text-muted">路由 {index + 1}</span>
                          <span className="text-sm font-semibold">{route.display_name_zh || "未命名模型"}</span>
                        </div>
                        <button
                          type="button"
                          title="删除路由"
                          aria-label={`删除路由 ${index + 1}`}
                          onClick={() => setEditor({ ...editor, routes: editor.routes.filter((_, itemIndex) => itemIndex !== index) })}
                          className="flex h-8 w-8 items-center justify-center rounded-md text-text-muted hover:bg-red-400/10 hover:text-red-300"
                        >
                          <Trash2 size={15} />
                        </button>
                      </div>
                      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                        <label className="space-y-1.5 text-xs text-text-secondary">
                          <span className="block">能力</span>
                          <select value={route.capability} onChange={(event) => updateRoute(index, { capability: event.target.value as AdminAICapability })} className="h-10 w-full rounded-md border border-glass-border bg-elevated px-3 text-sm text-foreground outline-none focus:border-primary/60">
                            {CAPABILITIES.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.id}</option>)}
                          </select>
                        </label>
                        {[
                          ["中文名称", "display_name_zh"],
                          ["供应商", "provider"],
                          ["模型 ID", "provider_model_id"],
                          ["凭据引用", "secret_ref"],
                        ].map(([label, key]) => (
                          <label key={key} className="space-y-1.5 text-xs text-text-secondary">
                            <span className="block">{label}</span>
                            <input value={String(route[key as keyof EditableRoute])} onChange={(event) => updateRoute(index, { [key]: event.target.value } as Partial<EditableRoute>)} className="h-10 w-full rounded-md border border-glass-border bg-elevated px-3 text-sm text-foreground outline-none focus:border-primary/60" />
                          </label>
                        ))}
                        <NumberField label="优先级" value={route.priority} onChange={(value) => updateRoute(index, { priority: value })} />
                      </div>
                      <div className="mt-3 flex flex-wrap gap-4 border-y border-border-subtle py-3">
                        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={route.enabled} onChange={(event) => updateRoute(index, { enabled: event.target.checked })} className="accent-primary" />启用路由</label>
                        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={route.is_primary} onChange={(event) => updateRoute(index, { is_primary: event.target.checked })} className="accent-primary" />设为主路由</label>
                      </div>
                      <div className="mt-4 grid gap-4 xl:grid-cols-2">
                        <JsonField label="默认参数（JSON）" value={route.defaultParametersText} onChange={(value) => updateRoute(index, { defaultParametersText: value })} />
                        <JsonField label="参数规则（JSON）" value={route.parameterSchemaText} onChange={(value) => updateRoute(index, { parameterSchemaText: value })} />
                        <JsonField label="计量公式（JSON）" value={route.meteringFormulaText} onChange={(value) => updateRoute(index, { meteringFormulaText: value })} />
                        <JsonField label="回退规则（JSON）" value={route.fallbackPolicyText} onChange={(value) => updateRoute(index, { fallbackPolicyText: value })} />
                      </div>
                    </article>
                  ))}
                </section>
              </div>
            ) : selected ? (
              <div className="space-y-6">
                <div className="flex flex-wrap items-start justify-between gap-4 border-b border-glass-border pb-4">
                  <div>
                    <div className="flex items-center gap-3">
                      <h2 className="font-mono text-xl font-semibold">V{selected.version_number}</h2>
                      <StatusBadge status={selected.status} />
                    </div>
                    <p className="mt-2 max-w-3xl text-sm leading-6 text-text-secondary">{selected.reason}</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setEditor(toEditableDraft(selected))}
                    className="inline-flex h-9 items-center gap-2 rounded-md border border-glass-border bg-glass px-3 text-sm text-text-secondary hover:bg-hover-bg hover:text-foreground"
                  >
                    <CopyPlus size={15} />
                    复制为草稿
                  </button>
                </div>

                <div className="grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
                  <div><p className="text-xs text-text-muted">创建时间</p><p className="mt-1 font-mono">{formatDate(selected.created_at)}</p></div>
                  <div><p className="text-xs text-text-muted">激活时间</p><p className="mt-1 font-mono">{formatDate(selected.activated_at)}</p></div>
                  <div><p className="text-xs text-text-muted">路由数量</p><p className="mt-1 font-mono">{selected.routes.length}</p></div>
                  <div><p className="text-xs text-text-muted">配置标识</p><p className="mt-1 truncate font-mono" title={selected.id}>{selected.id}</p></div>
                </div>

                {selected.status === "draft" && (
                  <section className="rounded-md border border-amber-400/20 bg-amber-400/5 p-4">
                    <label className="block text-xs font-medium text-text-secondary">变更原因</label>
                    <input value={actionReason} onChange={(event) => setActionReason(event.target.value)} placeholder="激活或停用前填写原因" className="mt-2 h-10 w-full rounded-md border border-glass-border bg-background/60 px-3 text-sm outline-none focus:border-primary/60" />
                    <div className="mt-3 flex flex-wrap gap-2">
                      <button type="button" disabled={busy !== null} onClick={() => void runAction("validate", () => adminConfigurationApi.validateVersion(selected.id), "配置校验通过")} className="inline-flex h-9 items-center gap-2 rounded-md border border-emerald-400/30 bg-emerald-400/10 px-3 text-sm text-emerald-200 hover:bg-emerald-400/15 disabled:opacity-50">
                        {busy === "validate" ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}校验配置
                      </button>
                      <button type="button" disabled={busy !== null} onClick={() => { const reason = requireActionReason(); if (reason) void runAction("activate", () => adminConfigurationApi.activateVersion(selected.id, reason), "配置已激活"); }} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50">
                        {busy === "activate" ? <Loader2 size={15} className="animate-spin" /> : <Zap size={15} />}激活版本
                      </button>
                      <button type="button" disabled={busy !== null} onClick={() => { const reason = requireActionReason(); if (reason) void runAction("disable", () => adminConfigurationApi.disableVersion(selected.id, reason), "草稿已停用"); }} className="inline-flex h-9 items-center gap-2 rounded-md border border-red-400/30 bg-red-400/10 px-3 text-sm text-red-200 hover:bg-red-400/15 disabled:opacity-50">
                        {busy === "disable" ? <Loader2 size={15} className="animate-spin" /> : <CircleOff size={15} />}停用草稿
                      </button>
                    </div>
                  </section>
                )}

                {selected.status !== "draft" && selected.status !== "active" && (
                  <section className="rounded-md border border-glass-border bg-glass p-4">
                    <label className="block text-xs font-medium text-text-secondary">回滚原因</label>
                    <div className="mt-2 flex flex-col gap-2 sm:flex-row">
                      <input value={actionReason} onChange={(event) => setActionReason(event.target.value)} placeholder="填写生成并激活新版本的原因" className="h-10 min-w-0 flex-1 rounded-md border border-glass-border bg-background/60 px-3 text-sm outline-none focus:border-primary/60" />
                      <button type="button" disabled={busy !== null} onClick={() => { const reason = requireActionReason(); if (reason) void runAction("rollback", () => adminConfigurationApi.rollbackVersion(selected.id, reason), "已生成并激活回滚版本"); }} className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-glass-border bg-glass px-3 text-sm hover:bg-hover-bg disabled:opacity-50">
                        {busy === "rollback" ? <Loader2 size={15} className="animate-spin" /> : <RotateCcw size={15} />}回滚为新版本
                      </button>
                    </div>
                  </section>
                )}

                <section className="border-y border-glass-border py-5">
                  <h3 className="mb-4 flex items-center gap-2 text-sm font-semibold"><Clock3 size={16} className="text-primary" />平台参数</h3>
                  <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 xl:grid-cols-3">
                    {[
                      ["算力券换算", `1 算力券 = ${selected.platform.tokens_per_ticket} 计量令牌`],
                      ["注册赠送", `${selected.platform.registration_initial_grant_microtickets} 微算力券`],
                      ["单用户 AI 并发", selected.platform.max_ai_concurrency_per_user],
                      ["单用户会话上限", selected.platform.max_sessions_per_user],
                      ["用户注册", registrationModeLabel(selected.platform.feature_flags.registration_mode)],
                      ["AI 新任务", selected.platform.feature_flags.new_ai_tasks_enabled ? "已开放" : "已暂停"],
                      ["回收保留", `${selected.platform.operational.soft_delete_retention_days} 天`],
                      ["工作进程并发（部署管理）", workerConcurrency ?? "-"],
                    ].map(([label, value]) => (
                      <div key={label} className="flex items-center justify-between gap-3 border-b border-border-subtle pb-2">
                        <span className="text-xs text-text-muted">{label}</span>
                        <span className="font-mono text-sm">{value}</span>
                      </div>
                    ))}
                  </div>
                  <div className="mt-4 flex flex-wrap gap-2">
                    {selected.platform.exposed_capabilities.map((capability) => (
                      <span key={capability} className="rounded border border-primary/20 bg-primary/10 px-2 py-1 text-xs text-primary">{capabilityLabel(capability)}</span>
                    ))}
                  </div>
                </section>

                <section>
                  <div className="mb-3 flex items-center gap-2">
                    <Database size={16} className="text-primary" />
                    <h3 className="text-sm font-semibold">模型路由</h3>
                  </div>
                  <div className="grid gap-3 xl:grid-cols-2">
                    {selected.routes.map((route) => (
                      <article key={`${route.capability}-${route.provider}-${route.provider_model_id}`} className="rounded-md border border-glass-border bg-glass p-4">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="truncate text-sm font-semibold">{route.display_name_zh}</p>
                            <p className="mt-1 truncate font-mono text-xs text-text-muted">{route.provider_model_id}</p>
                          </div>
                          <span className={`rounded border px-2 py-1 text-xs ${route.enabled ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300" : "border-border-subtle text-text-muted"}`}>{route.enabled ? "已启用" : "未启用"}</span>
                        </div>
                        <div className="mt-4 grid grid-cols-2 gap-3 text-xs">
                          <div><p className="text-text-muted">能力</p><p className="mt-1">{capabilityLabel(route.capability)}</p></div>
                          <div><p className="text-text-muted">供应商</p><p className="mt-1 font-mono">{route.provider}</p></div>
                          <div><p className="text-text-muted">优先级</p><p className="mt-1 font-mono">{route.priority}</p></div>
                          <div><p className="text-text-muted">计量类型</p><p className="mt-1 font-mono">{route.metering_formula.kind}</p></div>
                        </div>
                        <div className="mt-3 flex items-center justify-between border-t border-border-subtle pt-3 text-xs">
                          <span className="text-text-muted">凭据引用</span>
                          <span className="font-mono text-text-secondary">{route.secret_ref}</span>
                        </div>
                      </article>
                    ))}
                  </div>
                </section>
              </div>
            ) : (
              <div className="flex min-h-72 flex-col items-center justify-center rounded-md border border-dashed border-glass-border text-center">
                <Database size={24} className="text-text-muted" />
                <p className="mt-3 text-sm text-text-secondary">尚未创建平台配置</p>
                <button type="button" onClick={() => setEditor(toEditableDraft())} className="mt-4 inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent hover:bg-primary-hover"><Plus size={15} />创建首个草稿</button>
              </div>
            )}
          </main>
        </div>
      </div>
    </div>
  );
}
