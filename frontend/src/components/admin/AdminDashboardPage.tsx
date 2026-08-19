"use client";

import { useCallback, useEffect, useState } from "react";
import { Activity, ArrowRight, CircleAlert, RefreshCw, Ticket, Users, WalletCards } from "lucide-react";

import { adminPlatformApi, getSafeApiError, type AdminDashboard } from "@/lib/api";
import { toast } from "@/store/toastStore";

const PERIODS = [
  { days: 1, label: "今日" },
  { days: 7, label: "近 7 天" },
  { days: 30, label: "近 30 天" },
];

function money(fen: string): string {
  const value = BigInt(fen || "0");
  return `¥${(value / BigInt(100)).toString()}.${(value % BigInt(100)).toString().padStart(2, "0")}`;
}

function tickets(microtickets: string): string {
  const value = BigInt(microtickets || "0");
  const whole = value / BigInt(1_000_000);
  const fraction = (value % BigInt(1_000_000)).toString().padStart(6, "0").replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : whole.toString();
}

export default function AdminDashboardPage() {
  const [days, setDays] = useState(1);
  const [data, setData] = useState<AdminDashboard | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const end = new Date();
    const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
    try {
      setData(await adminPlatformApi.dashboard({ start_at: start.toISOString(), end_at: end.toISOString() }));
    } catch (error) {
      toast.error("运营概览加载失败", { body: getSafeApiError(error).message });
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => { void load(); }, [load]);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-glass-border pb-5">
        <div><p className="font-mono text-xs text-primary">系统运营</p><h1 className="mt-1 text-2xl font-semibold">运营概览</h1><p className="mt-1 text-sm text-text-secondary">用户、充值、算力消耗与需要人工介入的异常</p></div>
        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-md border border-glass-border bg-surface-inset p-1" role="group" aria-label="统计周期">
            {PERIODS.map((item) => <button key={item.days} type="button" aria-pressed={days === item.days} onClick={() => setDays(item.days)} className={`h-8 rounded px-3 text-xs ${days === item.days ? "bg-primary text-on-accent" : "text-text-secondary hover:bg-hover-bg hover:text-foreground"}`}>{item.label}</button>)}
          </div>
          <button type="button" title="刷新" aria-label="刷新" disabled={loading} onClick={() => void load()} className="grid h-10 w-10 place-items-center rounded-md border border-glass-border text-text-secondary hover:bg-hover-bg disabled:opacity-40"><RefreshCw size={16} className={loading ? "animate-spin" : ""} /></button>
        </div>
      </header>

      {loading && !data ? <div className="grid min-h-64 place-items-center text-sm text-text-muted">正在汇总运营数据</div> : data ? (
        <>
          <section className="grid border-y border-glass-border sm:grid-cols-2 xl:grid-cols-4" aria-label="运营指标">
            {[
              { label: "平台用户", value: data.users.total.toLocaleString("zh-CN"), detail: `新增 ${data.users.new}`, icon: Users },
              { label: "人工充值净额", value: money(data.orders.net_cash_fen), detail: `${data.orders.paid_count} 笔已入账`, icon: Ticket },
              { label: "净充值算力券", value: tickets(data.orders.net_ticket_microtickets), detail: "扣除已登记退款", icon: WalletCards },
              { label: "AI 任务", value: data.tasks.total.toLocaleString("zh-CN"), detail: `失败 ${data.tasks.failed} · 待复核 ${data.tasks.support_review}`, icon: Activity },
            ].map(({ label, value, detail, icon: Icon }, index) => (
              <div key={label} className={`min-w-0 px-5 py-5 ${index ? "border-t border-glass-border sm:border-l sm:border-t-0" : ""}`}><div className="flex items-center gap-2 text-xs text-text-muted"><Icon size={14} />{label}</div><strong className="mt-3 block truncate font-mono text-2xl font-medium">{value}</strong><span className="mt-2 block text-xs text-text-secondary">{detail}</span></div>
            ))}
          </section>

          <section aria-labelledby="exceptions-title">
            <div className="flex items-center justify-between border-b border-glass-border pb-3"><div><h2 id="exceptions-title" className="text-base font-semibold">待处理异常</h2><p className="mt-1 text-xs text-text-muted">已去重，按最近更新时间排序</p></div><button type="button" onClick={() => { window.location.hash = "#/admin/exceptions"; }} className="inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-xs text-primary hover:bg-primary/10">查看全部<ArrowRight size={14} /></button></div>
            {data.exceptions.items.length === 0 ? <div className="grid min-h-36 place-items-center border-b border-glass-border text-sm text-text-muted">当前没有需要人工处理的异常</div> : <div className="divide-y divide-glass-border border-b border-glass-border">{data.exceptions.items.map((item) => <button key={item.key} type="button" onClick={() => { if (item.user_id) window.location.hash = `#/admin/users/${item.user_id}`; }} className="grid w-full gap-2 py-3 text-left hover:bg-hover-bg sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center"><CircleAlert size={16} className={item.severity === "high" ? "text-red-300" : "text-amber-300"} /><div className="min-w-0"><p className="truncate text-sm">{item.summary}</p><p className="mt-1 font-mono text-xs text-text-muted">{item.resource_type} · {item.resource_id || "-"}</p></div><span className="text-xs text-text-muted">{item.updated_at ? new Date(item.updated_at).toLocaleString("zh-CN") : "-"}</span></button>)}</div>}
          </section>
          <p className="text-right font-mono text-[11px] text-text-muted">生成于 {new Date(data.generated_at).toLocaleString("zh-CN")}</p>
        </>
      ) : <div className="grid min-h-64 place-items-center text-sm text-text-muted">暂无运营数据</div>}
    </div>
  );
}
