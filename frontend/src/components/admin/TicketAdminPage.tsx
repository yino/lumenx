"use client";

import { useCallback, useState } from "react";
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Clock3,
  Gift,
  HandCoins,
  Loader2,
  RefreshCw,
  Search,
  Ticket,
  WalletCards,
} from "lucide-react";
import {
  adminTicketApi,
  type AdminTicketLedgerItem,
  type AdminTicketOperation,
  type AdminTicketWalletView,
} from "@/lib/api";

const OPERATION_COPY: Record<AdminTicketOperation, { label: string; action: string }> = {
  grant: { label: "赠送", action: "确认赠送" },
  debit: { label: "扣减", action: "确认扣减" },
  compensation: { label: "补偿", action: "确认补偿" },
};

const ENTRY_COPY: Record<AdminTicketLedgerItem["entry_type"], string> = {
  grant: "赠送",
  hold: "任务预扣",
  settlement: "任务结算",
  release: "释放预扣",
  adjustment: "人工调整",
  compensation: "人工补偿",
};

const ZERO_BIGINT = BigInt(0);
const MICROTICKETS_PER_TICKET = BigInt(1_000_000);

function errorMessage(error: unknown): string {
  const response = (error as {
    response?: { status?: number; data?: { message?: string; detail?: string } };
  })?.response;
  if (response?.data?.message) return response.data.message;
  if (response?.status === 404 && response.data?.detail === "Not Found") {
    return "算力券管理服务仅在云端模式可用";
  }
  if (typeof response?.data?.detail === "string") return response.data.detail;
  if (error instanceof Error) return error.message;
  return "操作失败，请稍后重试";
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function microticketsToText(value: string): string {
  const amount = BigInt(value);
  const negative = amount < ZERO_BIGINT;
  const absolute = negative ? -amount : amount;
  const whole = absolute / MICROTICKETS_PER_TICKET;
  const fraction = (absolute % MICROTICKETS_PER_TICKET)
    .toString()
    .padStart(6, "0")
    .replace(/0+$/, "");
  return `${negative ? "-" : ""}${whole}${fraction ? `.${fraction}` : ""}`;
}

function signedAvailableDelta(item: AdminTicketLedgerItem): {
  text: string;
  positive: boolean;
} {
  const delta = BigInt(item.available_delta);
  if (delta === ZERO_BIGINT) {
    const held = BigInt(item.held_delta);
    return {
      text: `${held > ZERO_BIGINT ? "+" : ""}${microticketsToText(item.held_delta)} 预扣`,
      positive: false,
    };
  }
  return {
    text: `${delta > ZERO_BIGINT ? "+" : ""}${microticketsToText(item.available_delta)}`,
    positive: delta > ZERO_BIGINT,
  };
}

export default function TicketAdminPage() {
  const [userId, setUserId] = useState("");
  const [loadedUserId, setLoadedUserId] = useState<string | null>(null);
  const [view, setView] = useState<AdminTicketWalletView | null>(null);
  const [operation, setOperation] = useState<AdminTicketOperation>("grant");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const loadWallet = useCallback(async (targetUserId: string) => {
    const normalized = targetUserId.trim();
    if (!normalized) {
      setNotice({ kind: "error", text: "请输入用户标识" });
      return;
    }
    setLoading(true);
    setNotice(null);
    try {
      const loaded = await adminTicketApi.getWallet(normalized);
      setView(loaded);
      setLoadedUserId(normalized);
    } catch (error) {
      setView(null);
      setLoadedUserId(null);
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setLoading(false);
    }
  }, []);

  const submitAdjustment = async () => {
    if (!loadedUserId) {
      setNotice({ kind: "error", text: "请先查询用户钱包" });
      return;
    }
    if (!amount.trim() || Number(amount) <= 0) {
      setNotice({ kind: "error", text: "请输入大于零的算力券数量" });
      return;
    }
    if (!reason.trim()) {
      setNotice({ kind: "error", text: "请填写本次调整原因" });
      return;
    }
    setSubmitting(true);
    setNotice(null);
    try {
      const result = await adminTicketApi.adjust(
        loadedUserId,
        operation,
        amount.trim(),
        reason.trim(),
      );
      const reloaded = await adminTicketApi.getWallet(loadedUserId);
      setView(reloaded);
      setAmount("");
      setReason("");
      setNotice({ kind: "success", text: result.message });
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-full bg-background text-foreground">
      <header className="border-b border-glass-border px-5 py-5 md:px-8">
        <div className="mx-auto flex max-w-[1380px] flex-wrap items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-primary/30 bg-primary/10 text-primary">
              <Ticket size={20} />
            </div>
            <div className="min-w-0">
              <h1 className="text-xl font-semibold">算力券管理</h1>
              <p className="mt-0.5 text-sm text-text-secondary">用户余额调整与账务流水</p>
            </div>
          </div>
          {loadedUserId && (
            <button
              type="button"
              title="刷新钱包"
              aria-label="刷新钱包"
              onClick={() => void loadWallet(loadedUserId)}
              disabled={loading}
              className="flex h-10 w-10 items-center justify-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw size={17} className={loading ? "animate-spin" : ""} />
            </button>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-[1380px] px-5 py-6 md:px-8">
        <div className="flex flex-col gap-3 border-b border-glass-border pb-6 sm:flex-row">
          <label className="min-w-0 flex-1">
            <span className="mb-1.5 block text-xs font-medium text-text-secondary">用户标识</span>
            <input
              value={userId}
              onChange={(event) => setUserId(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void loadWallet(userId);
              }}
              placeholder="输入用户 UUID"
              className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 font-mono text-sm outline-none focus:border-primary/60"
            />
          </label>
          <button
            type="button"
            onClick={() => void loadWallet(userId)}
            disabled={loading}
            className="mt-auto inline-flex h-10 items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50"
          >
            {loading ? <Loader2 size={16} className="animate-spin" /> : <Search size={16} />}
            查询钱包
          </button>
        </div>

        {notice && (
          <div className={`mt-4 flex items-start gap-2 rounded-md border px-3 py-2.5 text-sm ${notice.kind === "success" ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-200" : "border-red-400/25 bg-red-400/10 text-red-200"}`}>
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span>{notice.text}</span>
          </div>
        )}

        {view ? (
          <div className="mt-6 space-y-7">
            <section className="border-y border-glass-border bg-glass">
              <div className="grid sm:grid-cols-3">
                {[
                  ["可用算力券", view.wallet.available_tickets, WalletCards],
                  ["预扣算力券", view.wallet.held_tickets, Clock3],
                  ["算力券总额", view.wallet.total_tickets, Ticket],
                ].map(([label, value, Icon], index) => (
                  <div key={String(label)} className={`px-5 py-5 ${index > 0 ? "border-t border-glass-border sm:border-l sm:border-t-0" : ""}`}>
                    <div className="flex items-center gap-2 text-xs text-text-secondary">
                      <Icon size={15} />
                      <span>{String(label)}</span>
                    </div>
                    <p className="mt-2 font-mono text-2xl font-semibold">{String(value)}</p>
                  </div>
                ))}
              </div>
            </section>

            <div className="grid gap-8 xl:grid-cols-[360px_minmax(0,1fr)]">
              <section>
                <div className="mb-4 flex items-center gap-2">
                  <HandCoins size={17} className="text-primary" />
                  <h2 className="text-base font-semibold">人工调整</h2>
                </div>
                <div className="grid grid-cols-3 rounded-md border border-glass-border bg-glass p-1">
                  {(Object.keys(OPERATION_COPY) as AdminTicketOperation[]).map((item) => {
                    const Icon = item === "grant" ? Gift : item === "debit" ? ArrowDown : ArrowUp;
                    return (
                      <button
                        key={item}
                        type="button"
                        onClick={() => setOperation(item)}
                        className={`inline-flex h-9 items-center justify-center gap-1.5 rounded text-sm ${operation === item ? "bg-primary text-on-accent" : "text-text-secondary hover:bg-hover-bg hover:text-foreground"}`}
                      >
                        <Icon size={14} />
                        {OPERATION_COPY[item].label}
                      </button>
                    );
                  })}
                </div>
                <label className="mt-4 block">
                  <span className="mb-1.5 block text-xs font-medium text-text-secondary">算力券数量</span>
                  <input
                    type="number"
                    min="0.000001"
                    step="0.000001"
                    value={amount}
                    onChange={(event) => setAmount(event.target.value)}
                    placeholder="0.000000"
                    className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 font-mono text-sm outline-none focus:border-primary/60"
                  />
                </label>
                <label className="mt-4 block">
                  <span className="mb-1.5 block text-xs font-medium text-text-secondary">调整原因</span>
                  <textarea
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="填写可审计的中文原因"
                    className="min-h-24 w-full resize-y rounded-md border border-glass-border bg-glass px-3 py-2 text-sm leading-6 outline-none focus:border-primary/60"
                  />
                </label>
                <button
                  type="button"
                  onClick={() => void submitAdjustment()}
                  disabled={submitting}
                  className="mt-4 inline-flex h-10 w-full items-center justify-center gap-2 rounded-md bg-primary text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50"
                >
                  {submitting && <Loader2 size={16} className="animate-spin" />}
                  {OPERATION_COPY[operation].action}
                </button>
              </section>

              <section className="min-w-0">
                <div className="mb-4 flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <Clock3 size={17} className="text-primary" />
                    <h2 className="text-base font-semibold">账务流水</h2>
                  </div>
                  <span className="font-mono text-xs text-text-muted">{view.total} 条</span>
                </div>
                <div className="divide-y divide-glass-border border-y border-glass-border">
                  {view.ledger.length ? view.ledger.map((item) => {
                    const delta = signedAvailableDelta(item);
                    return (
                      <article key={item.id} className="grid gap-2 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-sm font-medium">{ENTRY_COPY[item.entry_type]}</span>
                            <span className="font-mono text-xs text-text-muted">{formatDate(item.created_at)}</span>
                          </div>
                          <p className="mt-1 truncate text-sm text-text-secondary" title={item.reason ?? ""}>{item.reason || "系统账务操作"}</p>
                          <p className="mt-1 truncate font-mono text-[11px] text-text-muted" title={item.id}>ID: {item.id}</p>
                        </div>
                        <div className="sm:text-right">
                          <p className={`font-mono text-sm font-semibold ${delta.positive ? "text-emerald-300" : "text-amber-300"}`}>{delta.text}</p>
                          <p className="mt-1 text-xs text-text-muted">可用余额 {microticketsToText(item.available_after)}</p>
                        </div>
                      </article>
                    );
                  }) : (
                    <div className="py-14 text-center text-sm text-text-muted">暂无账务流水</div>
                  )}
                </div>
              </section>
            </div>
          </div>
        ) : !loading && (
          <div className="flex min-h-72 flex-col items-center justify-center text-center text-text-muted">
            <WalletCards size={28} />
            <p className="mt-3 text-sm">查询用户后查看钱包与流水</p>
          </div>
        )}
      </main>
    </div>
  );
}
