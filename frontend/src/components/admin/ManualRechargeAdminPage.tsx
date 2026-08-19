"use client";

import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { ArrowLeft, CheckCircle2, CircleDollarSign, Download, Loader2, Plus, RefreshCw, RotateCcw, XCircle } from "lucide-react";

import {
  adminRechargeApi,
  adminPlatformApi,
  createIdempotencyKey,
  getSafeApiError,
  type ManualRechargeDetail,
  type ManualRechargeOrder,
  type ManualRechargePage,
  type ManualRechargeReconciliation,
  type ManualRechargeStatus,
} from "@/lib/api";
import { adminFilter, replaceAdminFilters } from "@/lib/adminRoute";
import { toast } from "@/store/toastStore";
import { AdminReasonDialog } from "./AdminDialogs";
import AdminPagination from "./AdminPagination";

const EMPTY_PAGE: ManualRechargePage = { items: [], total: 0, total_cash_fen: "0", total_ticket_microtickets: "0", offset: 0, limit: 30 };

function fenFromYuan(value: string): number | null {
  const match = value.trim().match(/^(\d{1,10})(?:\.(\d{1,2}))?$/);
  if (!match) return null;
  const result = BigInt(match[1]) * BigInt(100) + BigInt((match[2] || "").padEnd(2, "0") || "0");
  return result > BigInt(0) && result <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(result) : null;
}

function microticketsFromTickets(value: string): number | null {
  const match = value.trim().match(/^(\d{1,10})(?:\.(\d{1,6}))?$/);
  if (!match) return null;
  const result = BigInt(match[1]) * BigInt(1_000_000) + BigInt((match[2] || "").padEnd(6, "0") || "0");
  return result > BigInt(0) && result <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(result) : null;
}

function yuan(fen: string): string {
  const value = BigInt(fen || "0");
  return `¥${value / BigInt(100)}.${(value % BigInt(100)).toString().padStart(2, "0")}`;
}

function tickets(microtickets: string): string {
  const value = BigInt(microtickets || "0");
  const whole = value / BigInt(1_000_000);
  const fraction = (value % BigInt(1_000_000)).toString().padStart(6, "0").replace(/0+$/, "");
  return fraction ? `${whole}.${fraction}` : String(whole);
}

function badge(status: ManualRechargeStatus): string {
  if (status === "completed") return "border-emerald-400/25 bg-emerald-400/10 text-emerald-200";
  if (status === "pending") return "border-amber-400/25 bg-amber-400/10 text-amber-200";
  if (status === "cancelled") return "border-glass-border bg-glass text-text-muted";
  return "border-primary/25 bg-primary/10 text-primary";
}

function CreateOrderDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (order: ManualRechargeOrder) => void }) {
  const [userId, setUserId] = useState("");
  const [yuanValue, setYuanValue] = useState("");
  const [ticketsValue, setTicketsValue] = useState("");
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const titleId = useId();
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const cash = fenFromYuan(yuanValue);
    const tickets = microticketsFromTickets(ticketsValue);
    if (!/^\d+$/.test(userId) || !cash || !tickets || !/[\u3400-\u9fff]/.test(reason)) {
      toast.error("请检查用户 ID、金额、算力券数量和中文原因");
      return;
    }
    setBusy(true);
    try {
      const order = await adminRechargeApi.create({ user_id: Number(userId), cash_amount_fen: cash, ticket_amount_microtickets: tickets, offline_reference: reference.trim() || undefined, reason: reason.trim() }, createIdempotencyKey("recharge-create"));
      onCreated(order);
    } catch (error) {
      toast.error("充值订单创建失败", { body: getSafeApiError(error).message });
    } finally { setBusy(false); }
  };
  return <div className="fixed inset-0 z-[150] grid place-items-center bg-black/75 px-4 py-8 backdrop-blur-sm"><section role="dialog" aria-modal="true" aria-labelledby={titleId} className="w-full max-w-xl rounded-md border border-glass-border bg-elevated shadow-2xl"><header className="border-b border-glass-border px-5 py-4"><h2 id={titleId} className="font-semibold">新建人工充值订单</h2><p className="mt-1 text-sm text-text-secondary">只登记线下人工确认的购买，创建订单不会立即增加余额。</p></header><form onSubmit={submit} className="space-y-4 px-5 py-5"><div className="grid gap-4 sm:grid-cols-2"><label className="text-sm">用户 ID<input autoFocus inputMode="numeric" value={userId} onChange={(e) => setUserId(e.target.value.replace(/\D/g, ""))} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3 outline-none focus:border-primary/60" /></label><label className="text-sm">线下凭证号<input value={reference} onChange={(e) => setReference(e.target.value)} maxLength={160} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3 outline-none focus:border-primary/60" /></label><label className="text-sm">人民币金额（元）<input inputMode="decimal" placeholder="例如 199.00" value={yuanValue} onChange={(e) => setYuanValue(e.target.value)} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3 font-mono outline-none focus:border-primary/60" /></label><label className="text-sm">充值算力券<input inputMode="decimal" placeholder="最多 6 位小数" value={ticketsValue} onChange={(e) => setTicketsValue(e.target.value)} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3 font-mono outline-none focus:border-primary/60" /></label></div><label className="block text-sm">登记原因<textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} className="mt-2 w-full resize-none rounded-md border border-glass-border bg-input-bg px-3 py-2 outline-none focus:border-primary/60" /></label><footer className="flex justify-end gap-2 border-t border-glass-border pt-4"><button type="button" onClick={onClose} disabled={busy} className="h-9 rounded-md border border-glass-border px-3 text-sm">取消</button><button type="submit" disabled={busy} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent disabled:opacity-40">{busy ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />}创建待确认订单</button></footer></form></section></div>;
}

export default function ManualRechargeAdminPage({ orderId, initialUserId, queryString = "" }: { orderId?: string | null; initialUserId?: string | null; queryString?: string }) {
  const initialOffset = Number(adminFilter(queryString, "offset"));
  const initialLimit = Number(adminFilter(queryString, "limit"));
  const [page, setPage] = useState({
    ...EMPTY_PAGE,
    offset: Number.isSafeInteger(initialOffset) && initialOffset >= 0 ? initialOffset : 0,
    limit: [20, 30, 50, 100].includes(initialLimit) ? initialLimit : 30,
  });
  const [detail, setDetail] = useState<ManualRechargeDetail | null>(null);
  const [status, setStatus] = useState<"" | ManualRechargeStatus>(() => adminFilter(queryString, "status") as "" | ManualRechargeStatus);
  const [userId, setUserId] = useState(() => {
    const value = initialUserId || adminFilter(queryString, "user_id");
    return /^\d+$/.test(value || "") ? value || "" : "";
  });
  const [orderNumber, setOrderNumber] = useState(() => adminFilter(queryString, "order_number"));
  const [actorAdminId, setActorAdminId] = useState(() => adminFilter(queryString, "actor_admin_id"));
  const [reference, setReference] = useState(() => adminFilter(queryString, "offline_reference"));
  const [startAt, setStartAt] = useState(() => adminFilter(queryString, "start_at"));
  const [endAt, setEndAt] = useState(() => adminFilter(queryString, "end_at"));
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [action, setAction] = useState<"complete" | "cancel" | "refund" | "reconcile" | null>(null);
  const [busy, setBusy] = useState(false);
  const [refundCash, setRefundCash] = useState("");
  const [refundTickets, setRefundTickets] = useState("");
  const [exportOpen, setExportOpen] = useState(false);
  const [reconciliation, setReconciliation] = useState<ManualRechargeReconciliation | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      if (orderId) setDetail(await adminRechargeApi.get(orderId));
      else setPage(await adminRechargeApi.list({
        order_number: orderNumber.trim() || undefined,
        status: status || undefined,
        user_id: userId || undefined,
        actor_admin_id: actorAdminId || undefined,
        offline_reference: reference.trim() || undefined,
        start_at: startAt ? `${startAt}T00:00:00` : undefined,
        end_at: endAt ? `${endAt}T23:59:59` : undefined,
        offset: page.offset,
        limit: page.limit,
      }));
    } catch (error) { toast.error("充值订单加载失败", { body: getSafeApiError(error).message }); }
    finally { setLoading(false); }
  }, [actorAdminId, endAt, orderId, orderNumber, page.limit, page.offset, reference, startAt, status, userId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (orderId) return;
    replaceAdminFilters("recharge-orders", {
      order_number: orderNumber.trim(), user_id: userId, status,
      actor_admin_id: actorAdminId, offline_reference: reference.trim(),
      start_at: startAt, end_at: endAt,
      offset: page.offset || undefined,
      limit: page.limit === 30 ? undefined : page.limit,
    });
  }, [actorAdminId, endAt, orderId, orderNumber, page.limit, page.offset, reference, startAt, status, userId]);

  const runAction = async (reason: string) => {
    if (!detail || !action) return;
    setBusy(true);
    try {
      if (action === "complete") await adminRechargeApi.complete(detail.order.id, detail.order.version, reason);
      if (action === "cancel") await adminRechargeApi.cancel(detail.order.id, detail.order.version, reason);
      if (action === "refund") {
        const cash = fenFromYuan(refundCash); const tickets = microticketsFromTickets(refundTickets);
        if (!cash || !tickets) { toast.error("退款金额和算力券数量格式不正确"); return; }
        await adminRechargeApi.refund(detail.order.id, { expected_version: detail.order.version, cash_amount_fen: cash, ticket_amount_microtickets: tickets, reason });
      }
      if (action === "reconcile") setReconciliation(await adminRechargeApi.reconcile(detail.order.id, reason));
      toast.success(action === "reconcile" ? "对账报告已生成" : "订单状态已更新");
      setAction(null); setRefundCash(""); setRefundTickets(""); await load();
    } catch (error) {
      const safe = getSafeApiError(error);
      if (safe.code === "ORDER_VERSION_CONFLICT") {
        setDetail(await adminRechargeApi.get(detail.order.id));
        setAction(null);
        toast.error("订单已发生变化，已为你刷新最新状态");
      } else toast.error("订单操作失败", { body: safe.message });
    }
    finally { setBusy(false); }
  };

  const runExport = async (purpose: string) => {
    setBusy(true);
    try {
      const end = endAt ? new Date(`${endAt}T23:59:59`) : new Date();
      const start = startAt ? new Date(`${startAt}T00:00:00`) : new Date(end.getTime() - 31 * 86400_000);
      const blob = await adminPlatformApi.exportCsv("orders", {
        start_at: start.toISOString(), end_at: end.toISOString(), purpose,
        user_id: userId || undefined, status: status || undefined,
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = `人工充值订单-${new Date().toISOString().slice(0, 10)}.csv`; link.click();
      URL.revokeObjectURL(url);
      setExportOpen(false);
      toast.success("订单 CSV 已导出");
    } catch (error) { toast.error("订单导出失败", { body: getSafeApiError(error).message }); }
    finally { setBusy(false); }
  };

  if (orderId) return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-glass-border pb-5">
        <div className="flex items-center gap-3">
          <button type="button" aria-label="返回订单列表" title="返回" onClick={() => { window.location.hash = "#/admin/recharge-orders"; }} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border hover:bg-hover-bg"><ArrowLeft size={16} /></button>
          <div><p className="font-mono text-xs text-primary">人工充值订单</p><h1 className="mt-1 text-xl font-semibold">{detail?.order.order_number || orderId}</h1></div>
        </div>
        <button type="button" aria-label="刷新" title="刷新" onClick={() => void load()} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border"><RefreshCw size={16} className={loading ? "animate-spin" : ""} /></button>
      </header>
      {loading && !detail ? <div className="grid min-h-64 place-items-center text-text-muted">正在加载订单</div> : detail ? <>
        <section className="grid border-y border-glass-border sm:grid-cols-2 lg:grid-cols-4">
          {[["状态", detail.order.status_zh], ["线下金额", yuan(detail.order.cash_amount_fen)], ["充值算力券", detail.order.ticket_amount], ["用户 ID", detail.order.user_id]].map(([label, value], index) => <div key={label} className={`px-4 py-4 ${index ? "border-t border-glass-border sm:border-l sm:border-t-0" : ""}`}><span className="text-xs text-text-muted">{label}</span><strong className="mt-2 block font-mono text-base">{value}</strong></div>)}
        </section>
        <section className="grid gap-3 border-b border-glass-border pb-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <p><span className="block text-xs text-text-muted">线下凭证</span><strong className="mt-1 block font-mono">{detail.order.offline_reference || "未填写"}</strong></p>
          <p><span className="block text-xs text-text-muted">当前版本</span><strong className="mt-1 block font-mono">V{detail.order.version}</strong></p>
          <p><span className="block text-xs text-text-muted">累计退款</span><strong className="mt-1 block font-mono">{yuan(detail.order.refunded_cash_fen)} / {tickets(detail.order.refunded_microtickets)} 券</strong></p>
          <p><span className="block text-xs text-text-muted">创建时间</span><strong className="mt-1 block text-xs">{new Date(detail.order.created_at).toLocaleString("zh-CN")}</strong></p>
        </section>
        <div className="flex flex-wrap gap-2">
          {detail.order.status === "pending" && <><button type="button" onClick={() => setAction("complete")} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm text-on-accent"><CheckCircle2 size={15} />确认到账</button><button type="button" onClick={() => setAction("cancel")} className="inline-flex h-9 items-center gap-2 rounded-md border border-red-400/25 px-3 text-sm text-red-200"><XCircle size={15} />取消订单</button></>}
          {["completed", "partially_refunded"].includes(detail.order.status) && <button type="button" onClick={() => setAction("refund")} className="inline-flex h-9 items-center gap-2 rounded-md border border-primary/30 px-3 text-sm text-primary"><RotateCcw size={15} />登记退款</button>}
          <button type="button" onClick={() => setAction("reconcile")} className="h-9 rounded-md border border-glass-border px-3 text-sm">运行对账</button>
        </div>
        {reconciliation && <section className={`border-y px-4 py-3 text-sm ${reconciliation.status === "mismatch" ? "border-red-400/30 bg-red-400/5" : "border-emerald-400/30 bg-emerald-400/5"}`}><div className="flex flex-wrap items-center justify-between gap-2"><strong>{reconciliation.status_zh}</strong><span className="font-mono text-xs text-text-muted">报告 #{reconciliation.report_id} · {reconciliation.severity}</span></div>{reconciliation.status === "mismatch" && <pre className="mt-3 overflow-x-auto whitespace-pre-wrap text-xs text-red-100">{JSON.stringify(reconciliation.details, null, 2)}</pre>}<p className="mt-2 text-xs text-text-muted">对账仅生成报告，不会修改订单、钱包或账本历史。</p></section>}
        <section>
          <h2 className="border-b border-glass-border pb-3 text-base font-semibold">不可变事件</h2>
          <div className="divide-y divide-glass-border border-b border-glass-border">{detail.events.map((event) => <div key={event.id} className="grid gap-2 py-3 sm:grid-cols-[1fr_auto]"><div><p className="text-sm">{event.event_type_zh} · V{event.version_before} → V{event.version_after}</p><p className="mt-1 text-xs text-text-secondary">{event.reason}</p><p className="mt-1 font-mono text-xs text-text-muted">管理员 {event.actor_admin_id}{event.ledger_entry_id ? ` · 账本 #${event.ledger_entry_id}` : ""}</p></div><p className="font-mono text-xs text-text-muted">{new Date(event.created_at).toLocaleString("zh-CN")}</p></div>)}</div>
        </section>
        <section>
          <h2 className="border-b border-glass-border pb-3 text-base font-semibold">钱包与账本关联</h2>
          <div className="grid gap-3 py-4 text-sm sm:grid-cols-2"><p>可用算力券 <strong className="font-mono">{detail.wallet.available_tickets}</strong></p><p>预扣算力券 <strong className="font-mono">{detail.wallet.held_tickets}</strong></p></div>
          {detail.ledger.length === 0 ? <p className="border-y border-glass-border py-4 text-sm text-text-muted">待确认或已取消订单没有充值账本记录</p> : <div className="divide-y divide-glass-border border-y border-glass-border">{detail.ledger.map((entry) => <div key={entry.id} className="grid gap-2 py-3 text-sm sm:grid-cols-[1fr_auto]"><div><p className="font-mono">账本 #{entry.id} · {entry.entry_type}</p><p className="mt-1 text-xs text-text-secondary">{entry.reason || "系统记录"}</p></div><div className="sm:text-right"><strong className="font-mono">{tickets(entry.available_delta)} 券</strong><p className="mt-1 text-xs text-text-muted">余额 {tickets(entry.available_after)} · 预扣 {tickets(entry.held_after)}</p></div></div>)}</div>}
        </section>
      </> : null}
      {action && <AdminReasonDialog title={action === "complete" ? "确认线下款项到账" : action === "cancel" ? "取消待确认订单" : action === "refund" ? "登记人工退款" : "运行订单对账"} description={action === "complete" ? "此操作会立即向用户钱包增加算力券，无法通过编辑历史记录撤销。" : action === "refund" ? `最多可退 ${yuan(detail?.order.remaining_refundable_cash_fen || "0")}、${tickets(detail?.order.remaining_refundable_microtickets || "0")} 券。当前可用 ${detail?.wallet.available_tickets || "0"} 券，预扣 ${detail?.wallet.held_tickets || "0"} 券不可用于退款。` : "操作将写入不可变事件和审计记录。"} confirmLabel={action === "reconcile" ? "生成报告" : "确认操作"} danger={action === "cancel" || action === "refund"} busy={busy} onClose={() => setAction(null)} onConfirm={runAction}>{action === "refund" && <div className="grid gap-3 sm:grid-cols-2"><label className="text-sm">退款金额（元）<input value={refundCash} onChange={(event) => setRefundCash(event.target.value)} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3 font-mono" /></label><label className="text-sm">扣回算力券<input value={refundTickets} onChange={(event) => setRefundTickets(event.target.value)} className="mt-2 h-10 w-full rounded-md border border-glass-border bg-input-bg px-3 font-mono" /></label></div>}</AdminReasonDialog>}
    </div>
  );

  const resetOffset = () => setPage((current) => ({ ...current, offset: 0 }));
  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-glass-border pb-5">
        <div><p className="font-mono text-xs text-primary">财务管理</p><h1 className="mt-1 text-xl font-semibold">人工充值订单</h1><p className="mt-1 text-sm text-text-secondary">线下登记、人工确认、退款与账本对账；与赠送、补偿和调整严格分开</p></div>
        <div className="flex gap-2"><button type="button" aria-label="导出订单" title="导出 CSV" onClick={() => setExportOpen(true)} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border"><Download size={16} /></button><button type="button" aria-label="刷新" title="刷新" onClick={() => void load()} className="grid h-9 w-9 place-items-center rounded-md border border-glass-border"><RefreshCw size={16} className={loading ? "animate-spin" : ""} /></button><button type="button" onClick={() => setCreateOpen(true)} className="inline-flex h-9 items-center gap-2 rounded-md bg-primary px-3 text-sm font-medium text-on-accent"><Plus size={15} />新建订单</button></div>
      </header>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <input value={orderNumber} onChange={(event) => { setOrderNumber(event.target.value); resetOffset(); }} placeholder="订单号" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <input inputMode="numeric" value={userId} onChange={(event) => { setUserId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="用户 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <select value={status} onChange={(event) => { setStatus(event.target.value as "" | ManualRechargeStatus); resetOffset(); }} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm"><option value="">全部状态</option><option value="pending">待确认</option><option value="completed">已完成</option><option value="partially_refunded">部分退款</option><option value="refunded">已退款</option><option value="cancelled">已取消</option></select>
        <input inputMode="numeric" value={actorAdminId} onChange={(event) => { setActorAdminId(event.target.value.replace(/\D/g, "")); resetOffset(); }} placeholder="管理员 ID" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <input value={reference} onChange={(event) => { setReference(event.target.value); resetOffset(); }} placeholder="线下凭证号" className="h-9 rounded-md border border-glass-border bg-input-bg px-3 text-sm" />
        <label className="flex items-center gap-2 text-xs text-text-muted">开始日期<input type="date" value={startAt} onChange={(event) => { setStartAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-sm text-foreground" /></label>
        <label className="flex items-center gap-2 text-xs text-text-muted">结束日期<input type="date" value={endAt} onChange={(event) => { setEndAt(event.target.value); resetOffset(); }} className="h-9 min-w-0 flex-1 rounded-md border border-glass-border bg-elevated px-2 text-sm text-foreground" /></label>
      </div>
      <section className="grid border-y border-glass-border sm:grid-cols-3"><div className="px-4 py-4"><span className="text-xs text-text-muted">订单数</span><strong className="mt-2 block font-mono text-xl">{page.total}</strong></div><div className="border-t border-glass-border px-4 py-4 sm:border-l sm:border-t-0"><span className="text-xs text-text-muted">筛选金额</span><strong className="mt-2 block font-mono text-xl">{yuan(page.total_cash_fen)}</strong></div><div className="border-t border-glass-border px-4 py-4 sm:border-l sm:border-t-0"><span className="text-xs text-text-muted">筛选算力券</span><strong className="mt-2 block font-mono text-xl">{tickets(page.total_ticket_microtickets)}</strong></div></section>
      {loading ? <div className="grid min-h-56 place-items-center text-text-muted">正在加载订单</div> : page.items.length === 0 ? <div className="grid min-h-44 place-items-center border-y border-glass-border text-sm text-text-muted">没有符合条件的充值订单</div> : <div className="divide-y divide-glass-border border-y border-glass-border">{page.items.map((order) => <button type="button" key={order.id} onClick={() => { window.location.hash = `#/admin/recharge-orders/${order.id}`; }} className="grid w-full gap-3 py-4 text-left hover:bg-hover-bg sm:grid-cols-[minmax(220px,1fr)_140px_150px_110px] sm:items-center"><div><div className="flex flex-wrap items-center gap-2"><CircleDollarSign size={15} className="text-primary" /><span className="font-mono text-sm">{order.order_number}</span><span className={`rounded border px-2 py-0.5 text-xs ${badge(order.status)}`}>{order.status_zh}</span></div><p className="mt-1 font-mono text-xs text-text-muted">用户 {order.user_id} · 管理员 {order.created_by_admin_id} · {order.offline_reference || "无凭证号"}</p></div><div><span className="text-xs text-text-muted">线下金额</span><strong className="mt-1 block font-mono">{yuan(order.cash_amount_fen)}</strong></div><div><span className="text-xs text-text-muted">充值算力券</span><strong className="mt-1 block font-mono">{order.ticket_amount}</strong></div><span className="text-xs text-text-muted">{new Date(order.created_at).toLocaleString("zh-CN")}</span></button>)}</div>}
      <AdminPagination offset={page.offset} limit={page.limit} total={page.total} onChange={(offset) => setPage((current) => ({ ...current, offset }))} onLimitChange={(limit) => setPage((current) => ({ ...current, limit, offset: 0 }))} />
      {createOpen && <CreateOrderDialog onClose={() => setCreateOpen(false)} onCreated={(order) => { setCreateOpen(false); window.location.hash = `#/admin/recharge-orders/${order.id}`; }} />}
      {exportOpen && <AdminReasonDialog title="导出人工充值订单" description="导出沿用当前用户、状态和日期筛选，文件不包含线下凭证明细。导出行为会写入审计记录。" confirmLabel="导出 CSV" busy={busy} onClose={() => setExportOpen(false)} onConfirm={runExport} />}
    </div>
  );
}
