"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  FileClock,
  FolderInput,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
} from "lucide-react";

import {
  adminPlatformApi,
  getSafeApiError,
  type AdminImportBatchDetail,
  type AdminImportBatchItem,
  type AdminImportDryRunResult,
  type AdminPage,
} from "@/lib/api";
import { toast } from "@/store/toastStore";
import { AdminReasonDialog } from "./AdminDialogs";


const EMPTY_PAGE: AdminPage<AdminImportBatchItem> = {
  items: [],
  total: 0,
  offset: 0,
  limit: 30,
};

const STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  dry_run: "预检完成",
  running: "导入中",
  completed: "已完成",
  failed: "已失败",
  reverted: "已回滚",
};

const COUNT_LABELS: Record<string, string> = {
  series: "系列",
  project: "项目",
  asset: "资产",
  media: "媒体",
  playground_history: "创作实验室历史",
  pending: "待处理",
  running: "处理中",
  completed: "已写入",
  reused: "已复用",
  failed: "失败",
  reverted: "已回滚",
};

function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function shortId(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function CountSummary({ counts }: { counts: Record<string, number> }) {
  return (
    <div className="flex flex-wrap gap-x-5 gap-y-2 text-sm text-text-secondary">
      {Object.entries(counts).map(([key, value]) => (
        <span key={key}>
          {COUNT_LABELS[key] || key} <strong className="font-mono text-foreground">{value}</strong>
        </span>
      ))}
    </div>
  );
}

export default function ImportAdminPage() {
  const [page, setPage] = useState<AdminPage<AdminImportBatchItem>>(EMPTY_PAGE);
  const [status, setStatus] = useState("");
  const [targetUserId, setTargetUserId] = useState("");
  const [targetWorkspaceId, setTargetWorkspaceId] = useState("");
  const [sourceDirectory, setSourceDirectory] = useState("");
  const [includePlayground, setIncludePlayground] = useState(true);
  const [missingMediaPolicy, setMissingMediaPolicy] = useState<"reject" | "clear">("reject");
  const [dryRun, setDryRun] = useState<AdminImportDryRunResult | null>(null);
  const [detail, setDetail] = useState<AdminImportBatchDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [rollbackTargetId, setRollbackTargetId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPage(await adminPlatformApi.listImportBatches({ status: status || undefined }));
    } catch (error) {
      toast.error("导入批次加载失败", { body: getSafeApiError(error).message });
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    void load();
  }, [load]);

  const runDryRun = async () => {
    if (!targetUserId.trim() || !targetWorkspaceId.trim() || !sourceDirectory.trim()) {
      toast.error("预检信息不完整", { body: "请填写目标用户、目标工作区和源目录" });
      return;
    }
    setSubmitting(true);
    setDetail(null);
    try {
      const result = await adminPlatformApi.dryRunImport({
        target_user_id: targetUserId.trim(),
        target_workspace_id: targetWorkspaceId.trim(),
        source_directory: sourceDirectory.trim(),
        include_playground: includePlayground,
        missing_media_policy: missingMediaPolicy,
      });
      setDryRun(result);
      toast.success(result.ready ? "预检通过" : "预检发现问题", { body: result.message });
      await load();
    } catch (error) {
      toast.error("预检失败", { body: getSafeApiError(error).message });
    } finally {
      setSubmitting(false);
    }
  };

  const inspectBatch = async (batchId: string) => {
    setActiveBatchId(batchId);
    try {
      setDetail(await adminPlatformApi.getImportBatch(batchId));
      setDryRun(null);
    } catch (error) {
      toast.error("批次详情加载失败", { body: getSafeApiError(error).message });
    } finally {
      setActiveBatchId(null);
    }
  };

  const executeBatch = async (batchId: string) => {
    setActiveBatchId(batchId);
    try {
      const result = await adminPlatformApi.executeImport(batchId);
      toast.success("导入执行完成", {
        body: result.integrity_ok ? "完整性核对通过" : "请查看批次详情中的核对结果",
      });
      await load();
      await inspectBatch(batchId);
    } catch (error) {
      toast.error("导入执行失败", { body: getSafeApiError(error).message });
    } finally {
      setActiveBatchId(null);
    }
  };

  const rollbackBatch = async (reason: string) => {
    if (!rollbackTargetId) return;
    const batchId = rollbackTargetId;
    setActiveBatchId(batchId);
    try {
      const result = await adminPlatformApi.rollbackImport(batchId, reason);
      toast.success(result.message, {
        body: `已回滚 ${result.reverted_items} 项，保留 ${result.preserved_items} 项`,
      });
      setRollbackTargetId(null);
      await load();
      await inspectBatch(batchId);
    } catch (error) {
      toast.error("批次回滚失败", { body: getSafeApiError(error).message });
    } finally {
      setActiveBatchId(null);
    }
  };

  return (
    <div className="space-y-7">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-glass-border pb-5">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-md border border-primary/30 bg-primary/10 text-primary">
            <FolderInput size={20} />
          </div>
          <div>
            <h1 className="text-xl font-semibold">本地数据导入</h1>
            <p className="mt-0.5 text-sm text-text-secondary">预检、迁移、完整性核对与安全回滚</p>
          </div>
        </div>
        <button
          type="button"
          title="刷新批次"
          aria-label="刷新批次"
          onClick={() => void load()}
          disabled={loading}
          className="flex h-10 w-10 items-center justify-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw size={17} className={loading ? "animate-spin" : ""} />
        </button>
      </header>

      <section className="grid gap-4 border-b border-glass-border pb-7 lg:grid-cols-2">
        <label>
          <span className="mb-1.5 block text-xs font-medium text-text-secondary">目标用户 ID</span>
          <input inputMode="numeric" value={targetUserId} onChange={(event) => setTargetUserId(event.target.value.replace(/\D/g, ""))} className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 font-mono text-sm outline-none focus:border-primary/60" />
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-medium text-text-secondary">目标工作区 ID</span>
          <input inputMode="numeric" value={targetWorkspaceId} onChange={(event) => setTargetWorkspaceId(event.target.value.replace(/\D/g, ""))} className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 font-mono text-sm outline-none focus:border-primary/60" />
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-medium text-text-secondary">源目录</span>
          <input value={sourceDirectory} onChange={(event) => setSourceDirectory(event.target.value)} placeholder="例如：creator-a" className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 text-sm outline-none focus:border-primary/60" />
        </label>
        <label>
          <span className="mb-1.5 block text-xs font-medium text-text-secondary">缺失媒体处理</span>
          <select value={missingMediaPolicy} onChange={(event) => setMissingMediaPolicy(event.target.value as "reject" | "clear")} className="h-10 w-full rounded-md border border-glass-border bg-elevated px-3 text-sm outline-none focus:border-primary/60">
            <option value="reject">阻止导入</option>
            <option value="clear">清除缺失引用</option>
          </select>
        </label>
        <label className="flex min-h-10 items-center gap-2 text-sm text-text-secondary">
          <input type="checkbox" checked={includePlayground} onChange={(event) => setIncludePlayground(event.target.checked)} className="h-4 w-4 accent-primary" />
          导入创作实验室历史
        </label>
        <button type="button" onClick={() => void runDryRun()} disabled={submitting} className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50 lg:justify-self-end">
          {submitting ? <Loader2 size={16} className="animate-spin" /> : <FileClock size={16} />}
          开始预检
        </button>
      </section>

      {dryRun && (
        <section className="space-y-4 border-b border-glass-border pb-7">
          <div className="flex items-center gap-2">
            {dryRun.ready ? <CheckCircle2 size={18} className="text-emerald-300" /> : <AlertTriangle size={18} className="text-amber-300" />}
            <h2 className="font-medium">{dryRun.message}</h2>
          </div>
          <CountSummary counts={dryRun.planned_counts} />
          <p className="text-sm text-text-secondary">媒体容量 <strong className="font-mono text-foreground">{formatBytes(dryRun.media_bytes)}</strong></p>
          {dryRun.issues.length > 0 && (
            <div className="divide-y divide-glass-border border-y border-glass-border">
              {dryRun.issues.map((issue, index) => (
                <div key={`${issue.code}-${index}`} className="py-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2"><span className={issue.blocking ? "text-red-200" : "text-amber-200"}>{issue.blocking ? "阻断" : "警告"}</span><span>{issue.message}</span></div>
                  <p className="mt-1 truncate font-mono text-xs text-text-muted">{issue.source_key}</p>
                </div>
              ))}
            </div>
          )}
          {dryRun.ready && <button type="button" onClick={() => void executeBatch(dryRun.batch_id)} disabled={activeBatchId === dryRun.batch_id} className="inline-flex h-10 items-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50"><Play size={16} />执行导入</button>}
        </section>
      )}

      <section className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="font-medium">导入批次 <span className="font-mono text-sm text-text-muted">{page.total}</span></h2>
          <select value={status} onChange={(event) => setStatus(event.target.value)} className="h-9 rounded-md border border-glass-border bg-elevated px-3 text-sm outline-none focus:border-primary/60">
            <option value="">全部状态</option>
            {Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
        {loading ? (
          <div className="flex min-h-32 items-center justify-center text-text-muted"><Loader2 size={20} className="animate-spin" /></div>
        ) : page.items.length === 0 ? (
          <div className="flex min-h-32 items-center justify-center border-y border-glass-border text-sm text-text-muted">暂无导入批次</div>
        ) : (
          <div className="divide-y divide-glass-border border-y border-glass-border">
            {page.items.map((batch) => (
              <article key={batch.id} className="grid gap-4 py-4 lg:grid-cols-[minmax(0,1fr)_minmax(190px,.7fr)_auto] lg:items-center">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><strong className="font-mono text-sm">批次 {shortId(batch.id)}</strong><span className="rounded border border-glass-border bg-glass px-2 py-1 text-xs text-text-secondary">{batch.status_zh}</span></div>
                  <p className="mt-1 truncate font-mono text-xs text-text-muted">指纹 {shortId(batch.source_fingerprint)}</p>
                </div>
                <div className="text-xs text-text-secondary"><p>用户 {shortId(batch.target_user_id)}</p><p className="mt-1">工作区 {shortId(batch.target_workspace_id)}</p><p className="mt-1 text-text-muted">{formatDate(batch.created_at)}</p></div>
                <div className="flex items-center gap-2 lg:justify-end">
                  <button type="button" title="查看详情" aria-label="查看详情" onClick={() => void inspectBatch(batch.id)} disabled={activeBatchId === batch.id} className="flex h-9 w-9 items-center justify-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50">{activeBatchId === batch.id ? <Loader2 size={15} className="animate-spin" /> : <Eye size={15} />}</button>
                  {(batch.status === "dry_run" || batch.status === "failed") && <button type="button" title="执行导入" aria-label="执行导入" onClick={() => void executeBatch(batch.id)} disabled={activeBatchId === batch.id} className="flex h-9 w-9 items-center justify-center rounded-md border border-primary/30 bg-primary/10 text-primary hover:bg-primary/20 disabled:opacity-50"><Play size={15} /></button>}
                  {(batch.status === "completed" || batch.status === "failed") && <button type="button" title="回滚批次" aria-label="回滚批次" onClick={() => setRollbackTargetId(batch.id)} disabled={activeBatchId === batch.id} className="flex h-9 w-9 items-center justify-center rounded-md border border-amber-400/25 bg-amber-400/10 text-amber-200 hover:bg-amber-400/15 disabled:opacity-50"><RotateCcw size={15} /></button>}
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {detail && (
        <section className="space-y-4 border-t border-glass-border pt-6">
          <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-medium">批次详情 {shortId(detail.id)}</h2><span className="rounded border border-glass-border bg-glass px-2 py-1 text-xs text-text-secondary">{STATUS_LABELS[detail.status] || detail.status}</span></div>
          <CountSummary counts={detail.item_status_counts} />
          <div className="grid gap-2 text-sm text-text-secondary sm:grid-cols-2"><p>创建时间 <span className="text-foreground">{formatDate(detail.created_at)}</span></p><p>完成时间 <span className="text-foreground">{formatDate(detail.completed_at)}</span></p></div>
          {detail.rollback_reason && <p className="text-sm text-amber-200">回滚原因：{detail.rollback_reason}</p>}
          {detail.error_report && <p className="text-sm text-red-200">批次存在失败项，可修复源数据后继续执行。</p>}
        </section>
      )}
      {rollbackTargetId && <AdminReasonDialog title="回滚导入批次" description="系统只回滚尚未被继续使用的数据，已产生后续引用的数据会保留并写入报告。" confirmLabel="确认回滚" danger busy={activeBatchId === rollbackTargetId} onClose={() => setRollbackTargetId(null)} onConfirm={rollbackBatch} />}
    </div>
  );
}
