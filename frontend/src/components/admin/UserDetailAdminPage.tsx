"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, Eye, Image as ImageIcon, Loader2, RefreshCw, WalletCards, X } from "lucide-react";

import { adminPlatformApi, getSafeApiError, type AdminInspectionResource, type AdminPage, type AdminUserOverview } from "@/lib/api";
import { toast } from "@/store/toastStore";
import { AdminReasonDialog } from "./AdminDialogs";

type DetailTab = "overview" | "workspaces" | "content" | "assets" | "tasks" | "finance" | "security";

const TAB_LABELS: Array<[DetailTab, string]> = [
  ["overview", "概览"], ["workspaces", "工作区"], ["content", "项目与剧本"],
  ["assets", "角色与素材"], ["tasks", "AI 任务"], ["finance", "订单与账本"], ["security", "安全与审计"],
];

const COUNT_LABELS: Record<string, string> = {
  workspaces: "工作区",
  projects: "项目与剧本",
  series: "系列",
  assets: "角色与素材",
  media: "媒体",
  tasks: "AI 任务",
  usage: "用量记录",
  orders: "充值订单",
};

const TASK_STATUS_LABELS: Record<string, string> = {
  reserved: "已预留",
  queued: "排队中",
  running: "执行中",
  provider_succeeded: "供应商已完成",
  succeeded: "已完成",
  failed: "已失败",
  cancelled: "已取消",
  support_review: "计费待复核",
};

function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function dateText(value: unknown): string {
  if (typeof value !== "string" || !value) return "-";
  return new Date(value).toLocaleString("zh-CN");
}

function SensitiveViewer({ result, onClose }: { result: Record<string, unknown>; onClose: () => void }) {
  const imageUrl = typeof result.url === "string" ? result.url : null;
  return <div className="fixed inset-0 z-[170] grid place-items-center bg-black/80 px-4 py-8 backdrop-blur-sm"><section role="dialog" aria-modal="true" aria-label="敏感内容查看结果" className="max-h-full w-full max-w-3xl overflow-y-auto rounded-md border border-glass-border bg-elevated shadow-2xl"><header className="sticky top-0 flex items-center justify-between border-b border-glass-border bg-elevated px-5 py-4"><div><h2 className="font-semibold">审计查看结果</h2><p className="mt-1 text-xs text-text-muted">关闭后需重新填写用途才能再次访问</p></div><button type="button" autoFocus aria-label="关闭" title="关闭" onClick={onClose} className="grid h-8 w-8 place-items-center rounded-md hover:bg-hover-bg"><X size={17} /></button></header><div className="px-5 py-5">{imageUrl ? <img src={imageUrl} alt="用户私有媒体预览" className="mx-auto max-h-[65vh] max-w-full object-contain" /> : typeof result.text === "string" ? <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-7">{result.text}</pre> : <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs leading-6 text-text-secondary">{JSON.stringify(result, null, 2)}</pre>}</div></section></div>;
}

export default function UserDetailAdminPage({ userId }: { userId: string }) {
  const [overview, setOverview] = useState<AdminUserOverview | null>(null);
  const [tab, setTab] = useState<DetailTab>("overview");
  const [resource, setResource] = useState<AdminInspectionResource>("workspaces");
  const [page, setPage] = useState<AdminPage<Record<string, unknown>>>({ items: [], total: 0, offset: 0, limit: 30 });
  const [workspaces, setWorkspaces] = useState<Array<Record<string, unknown>>>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [assetType, setAssetType] = useState("");
  const [loading, setLoading] = useState(true);
  const [sensitive, setSensitive] = useState<{ type: "script" | "prompt" | "media"; id: string; workspaceId: string } | null>(null);
  const [sensitiveResult, setSensitiveResult] = useState<Record<string, unknown> | null>(null);
  const [sensitiveBusy, setSensitiveBusy] = useState(false);

  useEffect(() => { setWorkspaceId(""); setTab("overview"); setResource("workspaces"); }, [userId]);

  const resourceForTab = useMemo<AdminInspectionResource>(() => {
    if (tab === "workspaces") return "workspaces";
    if (tab === "content") return resource === "series" ? "series" : "projects";
    if (tab === "assets") return resource === "media" ? "media" : "assets";
    if (tab === "tasks") return "tasks";
    if (tab === "finance") return ["ledger", "usage"].includes(resource) ? resource : "orders";
    if (tab === "security") return resource === "audit" ? "audit" : "sessions";
    return "workspaces";
  }, [resource, tab]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      if (tab === "overview") {
        const [loaded, workspacePage] = await Promise.all([
          adminPlatformApi.userOverview(userId),
          adminPlatformApi.userResources(userId, "workspaces", { limit: 100 }),
        ]);
        setOverview(loaded);
        setWorkspaces(workspacePage.items);
      } else {
        setPage(await adminPlatformApi.userResources(userId, resourceForTab, {
          workspace_id: workspaceId || undefined,
          asset_type: resourceForTab === "assets" ? assetType || undefined : undefined,
          offset: page.offset,
          limit: page.limit,
        }));
      }
    } catch (error) { toast.error("用户运营数据加载失败", { body: getSafeApiError(error).message }); }
    finally { setLoading(false); }
  }, [assetType, page.limit, page.offset, resourceForTab, tab, userId, workspaceId]);
  useEffect(() => { void load(); }, [load]);

  const changeTab = (next: DetailTab) => {
    setTab(next); setPage((current) => ({ ...current, offset: 0 }));
    if (next === "content") setResource("projects");
    if (next === "assets") setResource("assets");
    if (next === "tasks") setResource("tasks");
    if (next === "finance") setResource("orders");
    if (next === "security") setResource("sessions");
  };

  const openSensitive = (row: Record<string, unknown>) => {
    const rowWorkspace = text(row.workspace_id);
    if (!/^\d+$/.test(rowWorkspace)) { toast.error("该资源缺少有效工作区范围"); return; }
    const type = resourceForTab === "projects" ? "script" : resourceForTab === "media" ? "media" : "prompt";
    setSensitive({ type, id: text(row.id), workspaceId: rowWorkspace });
  };
  const viewSensitive = async (purpose: string) => {
    if (!sensitive) return; setSensitiveBusy(true);
    try {
      const result = sensitive.type === "script"
        ? await adminPlatformApi.viewScript(userId, sensitive.id, sensitive.workspaceId, purpose)
        : sensitive.type === "media"
          ? await adminPlatformApi.previewMedia(userId, sensitive.id, sensitive.workspaceId, purpose)
          : await adminPlatformApi.viewTaskPrompt(userId, sensitive.id, sensitive.workspaceId, purpose);
      setSensitive(null); setSensitiveResult(result as Record<string, unknown>);
    } catch (error) { toast.error("敏感内容查看失败", { body: getSafeApiError(error).message }); }
    finally { setSensitiveBusy(false); }
  };

  return <div className="space-y-5"><header className="flex flex-wrap items-center justify-between gap-4 border-b border-glass-border pb-5"><div className="flex items-center gap-3"><button type="button" aria-label="返回用户列表" title="返回" onClick={() => { window.location.hash = "#/admin/users"; }} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border hover:bg-hover-bg"><ArrowLeft size={16} /></button><div><p className="font-mono text-xs text-primary">用户运营详情</p><div className="mt-1 flex flex-wrap items-center gap-2"><h1 className="text-xl font-semibold">{overview?.account.username || overview?.account.phone_masked || `用户 ${userId}`}</h1>{overview && <span className={`rounded border px-2 py-0.5 text-xs ${overview.account.status === "active" ? "border-emerald-400/25 text-emerald-200" : "border-red-400/25 text-red-200"}`}>{overview.account.status === "active" ? "正常" : "已停用"}</span>}</div><p className="mt-1 font-mono text-xs text-text-muted">用户 ID {userId}{overview?.account.phone_masked ? ` · ${overview.account.phone_masked}` : ""}</p></div></div><div className="flex gap-2"><button type="button" onClick={() => { window.location.hash = `#/admin/recharge-orders?user_id=${userId}`; }} className="inline-flex h-9 items-center gap-2 rounded-md border border-primary/30 px-3 text-sm text-primary"><WalletCards size={15} />充值与订单</button><button type="button" title="刷新" aria-label="刷新" onClick={() => void load()} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border"><RefreshCw size={16} className={loading ? "animate-spin" : ""} /></button></div></header><div className="overflow-x-auto border-b border-glass-border"><div className="flex min-w-max gap-1" role="tablist" aria-label="用户详情分区">{TAB_LABELS.map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => changeTab(id)} className={`h-10 border-b-2 px-3 text-sm ${tab === id ? "border-primary text-foreground" : "border-transparent text-text-secondary hover:text-foreground"}`}>{label}</button>)}</div></div>{tab !== "overview" && <div className="flex flex-wrap gap-2"><select value={workspaceId} onChange={(e) => { setWorkspaceId(e.target.value); setPage((p) => ({ ...p, offset: 0 })); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="">全部工作区</option>{workspaces.map((item) => <option key={text(item.id)} value={text(item.id)}>{text(item.name)}</option>)}</select>{tab === "content" && <select value={resourceForTab} onChange={(e) => setResource(e.target.value as AdminInspectionResource)} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="projects">项目与剧本</option><option value="series">系列</option></select>}{tab === "assets" && <><select value={resourceForTab} onChange={(e) => setResource(e.target.value as AdminInspectionResource)} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="assets">角色、场景与道具</option><option value="media">媒体</option></select>{resourceForTab === "assets" && <select value={assetType} onChange={(e) => setAssetType(e.target.value)} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="">全部资产</option><option value="character">角色</option><option value="scene">场景</option><option value="prop">道具</option><option value="voice">声音</option></select>}</>}{tab === "finance" && <select value={resourceForTab} onChange={(e) => setResource(e.target.value as AdminInspectionResource)} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="orders">充值订单</option><option value="ledger">钱包账本</option><option value="usage">用量</option></select>}{tab === "security" && <select value={resourceForTab} onChange={(e) => setResource(e.target.value as AdminInspectionResource)} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="sessions">会话</option><option value="audit">审计</option></select>}</div>}{loading ? <div className="grid min-h-56 place-items-center text-text-muted"><Loader2 size={17} className="animate-spin" /></div> : tab === "overview" && overview ? <><section className="grid border-y border-glass-border sm:grid-cols-2 lg:grid-cols-4">{[["可用算力券", overview.wallet.available_tickets], ["预扣算力券", overview.wallet.held_tickets], ["工作区", overview.counts.workspaces || 0], ["待处理异常", overview.exceptions.total]].map(([label, value], i) => <div key={label} className={`px-4 py-4 ${i ? "border-t border-glass-border sm:border-l sm:border-t-0" : ""}`}><span className="text-xs text-text-muted">{label}</span><strong className="mt-2 block font-mono text-xl">{value}</strong></div>)}</section><section><h2 className="border-b border-glass-border pb-3 text-base font-semibold">资源统计</h2><div className="grid grid-cols-2 gap-px bg-glass-border sm:grid-cols-4">{Object.entries(overview.counts).map(([key, value]) => <div key={key} className="bg-background px-3 py-4"><span className="text-xs text-text-muted">{COUNT_LABELS[key] || "其他记录"}</span><strong className="mt-1 block font-mono">{value}</strong></div>)}</div></section><section><div className="flex items-end justify-between border-b border-glass-border pb-3"><div><h2 className="text-base font-semibold">最近活动</h2><p className="mt-1 text-xs text-text-muted">最近更新的 AI 任务和人工充值订单</p></div><span className={`text-xs ${overview.exceptions.total ? "text-amber-200" : "text-text-muted"}`}>任务异常 {overview.exceptions.tasks} · 订单异常 {overview.exceptions.orders}</span></div><div className="grid gap-6 py-4 lg:grid-cols-2"><div><h3 className="text-sm font-medium">AI 任务</h3>{overview.recent_activity.tasks.length ? <div className="mt-2 divide-y divide-glass-border">{overview.recent_activity.tasks.map((item) => <button key={text(item.id)} type="button" onClick={() => changeTab("tasks")} className="flex w-full items-center justify-between gap-3 py-3 text-left hover:bg-hover-bg"><span className="min-w-0"><span className="block truncate text-sm">{text(item.capability)}</span><span className="mt-1 block font-mono text-xs text-text-muted">任务 {text(item.id)} · 工作区 {text(item.workspace_id)}</span></span><span className="shrink-0 text-xs text-text-secondary">{TASK_STATUS_LABELS[text(item.status)] || "状态待确认"}<br />{dateText(item.updated_at)}</span></button>)}</div> : <p className="mt-3 text-sm text-text-muted">暂无 AI 任务</p>}</div><div><h3 className="text-sm font-medium">人工充值订单</h3>{overview.recent_activity.orders.length ? <div className="mt-2 divide-y divide-glass-border">{overview.recent_activity.orders.map((item) => <button key={text(item.id)} type="button" onClick={() => { window.location.hash = `#/admin/recharge-orders/${text(item.id)}`; }} className="flex w-full items-center justify-between gap-3 py-3 text-left hover:bg-hover-bg"><span className="min-w-0"><span className="block truncate font-mono text-sm">{text(item.order_number)}</span><span className="mt-1 block text-xs text-text-secondary">{text(item.status_zh)}</span></span><span className="shrink-0 text-xs text-text-muted">{dateText(item.updated_at)}</span></button>)}</div> : <p className="mt-3 text-sm text-text-muted">暂无充值订单</p>}</div></div></section></> : page.items.length === 0 ? <div className="grid min-h-44 place-items-center border-y border-glass-border text-sm text-text-muted">该分区暂无数据</div> : <div className="divide-y divide-glass-border border-y border-glass-border">{page.items.map((row) => <article key={text(row.id)} className="flex flex-col gap-3 py-4 lg:flex-row lg:items-center"><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-x-4 gap-y-1"><strong className="text-sm">{text(row.title || row.name || row.order_number || row.capability || row.action || row.mime_type || `#${text(row.id)}`)}</strong>{row.status ? <span className="rounded border border-glass-border px-2 py-0.5 text-xs text-text-secondary">{text(row.status_zh || row.status)}</span> : null}</div><p className="mt-2 line-clamp-2 font-mono text-xs text-text-muted">ID {text(row.id)} · 工作区 {text(row.workspace_id)} · {text(row.updated_at || row.created_at)}</p></div>{["projects", "media", "tasks"].includes(resourceForTab) && <button type="button" onClick={() => openSensitive(row)} className="inline-flex h-8 items-center gap-1.5 self-start rounded-md border border-glass-border px-2.5 text-xs text-text-secondary hover:bg-hover-bg lg:self-auto">{resourceForTab === "media" ? <ImageIcon size={13} /> : <Eye size={13} />}{resourceForTab === "projects" ? "查看剧本" : resourceForTab === "media" ? "预览" : "查看提示词"}</button>}</article>)}</div>}{tab !== "overview" && <div className="flex justify-end gap-2"><button disabled={page.offset === 0} onClick={() => setPage((p) => ({ ...p, offset: Math.max(0, p.offset - p.limit) }))} className="h-8 rounded-md border border-glass-border px-3 text-xs disabled:opacity-30">上一页</button><button disabled={page.offset + page.limit >= page.total} onClick={() => setPage((p) => ({ ...p, offset: p.offset + p.limit }))} className="h-8 rounded-md border border-glass-border px-3 text-xs disabled:opacity-30">下一页</button></div>}{sensitive && <AdminReasonDialog title={sensitive.type === "script" ? "查看完整剧本" : sensitive.type === "media" ? "预览私有媒体" : "查看任务提示词"} description="这是敏感内容访问。系统会记录管理员、目标用户、资源、用途和请求关联 ID。" confirmLabel="审计并查看" busy={sensitiveBusy} onClose={() => setSensitive(null)} onConfirm={viewSensitive} />}{sensitiveResult && <SensitiveViewer result={sensitiveResult} onClose={() => setSensitiveResult(null)} />}</div>;
}
