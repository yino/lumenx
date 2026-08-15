"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Coins,
  Loader2,
  ReceiptText,
  RefreshCw,
  Ticket,
  WalletCards,
} from "lucide-react";
import {
  userTicketApi,
  type UserTicketHistoryResponse,
  type UserTicketLedgerItem,
  type UserTicketUsageItem,
  type UserTicketWallet,
} from "@/lib/api";

type WalletTab = "ledger" | "usage";

const PAGE_SIZE = 20;

const CAPABILITY_COPY: Record<string, string> = {
  "script.analysis": "剧本分析",
  "prompt.polish": "提示词润色",
  "image.t2i": "文生图",
  "image.i2i": "图生图",
  "video.t2v": "文生视频",
  "video.i2v": "图生视频",
  "video.r2v": "参考生视频",
  "video.v2v": "视频编辑",
  "speech.tts": "语音合成",
  "audio.sfx": "音效生成",
};

function errorMessage(error: unknown): string {
  const response = (error as {
    response?: { status?: number; data?: { message?: string; detail?: string } };
  })?.response;
  if (response?.data?.message) return response.data.message;
  if (response?.status === 404 && response.data?.detail === "Not Found") {
    return "算力券服务仅在云端模式可用";
  }
  if (typeof response?.data?.detail === "string") return response.data.detail;
  if (error instanceof Error) return error.message;
  return "加载失败，请稍后重试";
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function ScopeLine({ workspaceId, projectId }: {
  workspaceId?: string | null;
  projectId?: string | null;
}) {
  if (!workspaceId && !projectId) return null;
  return (
    <p className="mt-1 truncate font-mono text-[11px] text-text-muted">
      {workspaceId ? `工作区 ${workspaceId}` : ""}
      {workspaceId && projectId ? " · " : ""}
      {projectId ? `项目 ${projectId}` : ""}
    </p>
  );
}

function LedgerRow({ item }: { item: UserTicketLedgerItem }) {
  const positive = item.display_delta_microtickets.startsWith("-") === false;
  const delta = item.display_delta_tickets === "0"
    ? "0"
    : `${positive ? "+" : ""}${item.display_delta_tickets}`;
  return (
    <article className="grid gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{item.operation_zh}</span>
          <span className="rounded border border-glass-border bg-glass px-1.5 py-0.5 text-[11px] text-text-secondary">{item.status_zh}</span>
          <span className="text-xs text-text-muted">{formatDate(item.created_at)}</span>
        </div>
        <p className="mt-1 truncate text-sm text-text-secondary" title={item.reason ?? ""}>{item.reason || "系统账务操作"}</p>
        <ScopeLine workspaceId={item.workspace_id} projectId={item.project_id} />
        {item.metering_tokens !== null && item.metering_tokens !== undefined && (
          <p className="mt-1 font-mono text-[11px] text-text-muted">计量令牌 {item.metering_tokens}</p>
        )}
      </div>
      <div className="sm:text-right">
        <p className={`font-mono text-sm font-semibold ${positive ? "text-emerald-300" : "text-amber-300"}`}>{delta}</p>
        <p className="mt-1 text-xs text-text-muted">入账后可用 {item.available_after_tickets} 算力券</p>
      </div>
    </article>
  );
}

function UsageRow({ item }: { item: UserTicketUsageItem }) {
  return (
    <article className="grid gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{CAPABILITY_COPY[item.capability] ?? item.capability}</span>
          <span className="rounded border border-glass-border bg-glass px-1.5 py-0.5 text-[11px] text-text-secondary">{item.status_zh}</span>
          <span className="text-xs text-text-muted">{formatDate(item.created_at)}</span>
        </div>
        <ScopeLine workspaceId={item.workspace_id} projectId={item.project_id} />
        <p className="mt-1 truncate font-mono text-[11px] text-text-muted" title={item.task_id}>任务 {item.task_id}</p>
      </div>
      <div className="sm:text-right">
        <p className="font-mono text-sm font-semibold text-amber-300">-{item.charged_tickets}</p>
        <p className="mt-1 font-mono text-xs text-text-muted">计量令牌 {item.metering_tokens}</p>
      </div>
    </article>
  );
}

export default function WalletPage() {
  const [wallet, setWallet] = useState<UserTicketWallet | null>(null);
  const [tab, setTab] = useState<WalletTab>("ledger");
  const [page, setPage] = useState(0);
  const [history, setHistory] = useState<UserTicketHistoryResponse<UserTicketLedgerItem | UserTicketUsageItem> | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (selectedTab: WalletTab, selectedPage: number) => {
    setLoading(true);
    setNotice(null);
    try {
      const offset = selectedPage * PAGE_SIZE;
      const [loadedWallet, loadedHistory] = await Promise.all([
        userTicketApi.getWallet(),
        selectedTab === "ledger"
          ? userTicketApi.getLedger(offset, PAGE_SIZE)
          : userTicketApi.getUsage(offset, PAGE_SIZE),
      ]);
      setWallet(loadedWallet);
      setHistory(loadedHistory);
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(tab, page);
  }, [load, page, tab]);

  const selectTab = (nextTab: WalletTab) => {
    setTab(nextTab);
    setPage(0);
  };
  const totalPages = Math.max(1, Math.ceil((history?.total ?? 0) / PAGE_SIZE));

  return (
    <div className="min-h-full bg-background text-foreground">
      <header className="border-b border-glass-border px-5 py-5 md:px-8">
        <div className="mx-auto flex max-w-[1280px] items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-primary/30 bg-primary/10 text-primary">
              <WalletCards size={20} />
            </div>
            <div>
              <h1 className="text-xl font-semibold">我的算力券</h1>
              <p className="mt-0.5 text-sm text-text-secondary">余额、预扣与 AI 用量</p>
            </div>
          </div>
          <button
            type="button"
            title="刷新算力券"
            aria-label="刷新算力券"
            disabled={loading}
            onClick={() => void load(tab, page)}
            className="flex h-10 w-10 items-center justify-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw size={17} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </header>

      <main className="mx-auto max-w-[1280px] px-5 py-6 md:px-8">
        {notice && (
          <div className="mb-5 flex items-start gap-2 rounded-md border border-red-400/25 bg-red-400/10 px-3 py-2.5 text-sm text-red-200">
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            <span>{notice}</span>
          </div>
        )}

        {wallet && (
          <section className="border-y border-glass-border bg-glass">
            <div className="grid sm:grid-cols-3">
              {[
                ["可用算力券", wallet.available_tickets, Coins],
                ["预扣算力券", wallet.held_tickets, Clock3],
                ["算力券总额", wallet.total_tickets, Ticket],
              ].map(([label, value, Icon], index) => (
                <div key={String(label)} className={`px-5 py-5 ${index > 0 ? "border-t border-glass-border sm:border-l sm:border-t-0" : ""}`}>
                  <div className="flex items-center gap-2 text-xs text-text-secondary"><Icon size={15} /><span>{String(label)}</span></div>
                  <p className="mt-2 font-mono text-2xl font-semibold">{String(value)}</p>
                </div>
              ))}
            </div>
          </section>
        )}

        <section className="mt-7">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-glass-border pb-3">
            <div className="grid grid-cols-2 rounded-md border border-glass-border bg-glass p-1">
              <button type="button" onClick={() => selectTab("ledger")} className={`inline-flex h-9 items-center justify-center gap-2 rounded px-3 text-sm ${tab === "ledger" ? "bg-primary text-on-accent" : "text-text-secondary hover:bg-hover-bg hover:text-foreground"}`}>
                <ReceiptText size={15} />账务流水
              </button>
              <button type="button" onClick={() => selectTab("usage")} className={`inline-flex h-9 items-center justify-center gap-2 rounded px-3 text-sm ${tab === "usage" ? "bg-primary text-on-accent" : "text-text-secondary hover:bg-hover-bg hover:text-foreground"}`}>
                <Clock3 size={15} />用量明细
              </button>
            </div>
            {history && <span className="font-mono text-xs text-text-muted">共 {history.total} 条</span>}
          </div>

          {loading && !history ? (
            <div className="flex min-h-64 items-center justify-center text-text-muted"><Loader2 size={22} className="animate-spin" /></div>
          ) : history?.items.length ? (
            <div className="divide-y divide-glass-border">
              {tab === "ledger"
                ? (history.items as UserTicketLedgerItem[]).map((item) => <LedgerRow key={item.id} item={item} />)
                : (history.items as UserTicketUsageItem[]).map((item) => <UsageRow key={item.id} item={item} />)}
            </div>
          ) : (
            <div className="flex min-h-64 flex-col items-center justify-center text-center text-text-muted">
              <ReceiptText size={28} />
              <p className="mt-3 text-sm">暂无{tab === "ledger" ? "账务流水" : "用量记录"}</p>
            </div>
          )}

          {history && history.total > PAGE_SIZE && (
            <div className="flex items-center justify-end gap-2 border-t border-glass-border pt-4">
              <button type="button" title="上一页" aria-label="上一页" disabled={page === 0 || loading} onClick={() => setPage((current) => Math.max(0, current - 1))} className="flex h-9 w-9 items-center justify-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg disabled:opacity-40"><ChevronLeft size={16} /></button>
              <span className="min-w-20 text-center font-mono text-xs text-text-muted">{page + 1} / {totalPages}</span>
              <button type="button" title="下一页" aria-label="下一页" disabled={page + 1 >= totalPages || loading} onClick={() => setPage((current) => current + 1)} className="flex h-9 w-9 items-center justify-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg disabled:opacity-40"><ChevronRight size={16} /></button>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
