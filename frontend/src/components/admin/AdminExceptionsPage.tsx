"use client";

import { useCallback, useEffect, useState } from "react";
import { CircleAlert, RefreshCw } from "lucide-react";

import { adminPlatformApi, getSafeApiError, type AdminExceptionItem } from "@/lib/api";
import { toast } from "@/store/toastStore";

export default function AdminExceptionsPage() {
  const [items, setItems] = useState<AdminExceptionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const load = useCallback(async () => {
    setLoading(true);
    try { setItems((await adminPlatformApi.exceptions(200)).items); }
    catch (error) { toast.error("异常队列加载失败", { body: getSafeApiError(error).message }); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  return <div className="space-y-5"><header className="flex items-end justify-between gap-4 border-b border-glass-border pb-5"><div><p className="font-mono text-xs text-primary">审计与运维</p><h1 className="mt-1 text-xl font-semibold">异常队列</h1><p className="mt-1 text-sm text-text-secondary">失败、停滞、计费待复核、对账差异和缺失媒体</p></div><button type="button" aria-label="刷新" title="刷新" onClick={() => void load()} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border"><RefreshCw size={16} className={loading ? "animate-spin" : ""} /></button></header>{loading ? <div className="grid min-h-56 place-items-center text-text-muted">正在加载异常</div> : items.length === 0 ? <div className="grid min-h-44 place-items-center border-y border-glass-border text-sm text-text-muted">当前没有需要处理的异常</div> : <div className="divide-y divide-glass-border border-y border-glass-border">{items.map((item) => <button type="button" key={item.key} onClick={() => { if (item.user_id) window.location.hash = `#/admin/users/${item.user_id}`; }} className="grid w-full gap-3 py-4 text-left hover:bg-hover-bg md:grid-cols-[auto_minmax(0,1fr)_160px] md:items-center"><CircleAlert size={17} className={item.severity === "high" ? "text-red-300" : "text-amber-300"} /><div><p className="text-sm">{item.summary}</p><p className="mt-1 font-mono text-xs text-text-muted">{item.kind} · {item.resource_type} {item.resource_id || "-"} · 用户 {item.user_id || "-"}</p></div><span className="text-xs text-text-muted md:text-right">{item.updated_at ? new Date(item.updated_at).toLocaleString("zh-CN") : "-"}</span></button>)}</div>}</div>;
}
