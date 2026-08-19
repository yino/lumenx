"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Clipboard, KeyRound, Loader2, RefreshCw, ShieldX } from "lucide-react";

import {
  adminPlatformApi,
  getSafeApiError,
  type AdminInvitation,
} from "@/lib/api";
import { toast } from "@/store/toastStore";
import { AdminReasonDialog } from "./AdminDialogs";

const STATUS_LABEL: Record<AdminInvitation["status"], string> = {
  active: "有效",
  consumed: "已使用",
  revoked: "已撤销",
  expired: "已过期",
};

function dateText(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function defaultExpiry(): string {
  const value = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
  value.setSeconds(0, 0);
  const offset = value.getTimezoneOffset() * 60_000;
  return new Date(value.getTime() - offset).toISOString().slice(0, 16);
}

export default function InvitationAdminPage() {
  const [items, setItems] = useState<AdminInvitation[]>([]);
  const [phone, setPhone] = useState("");
  const [expiresAt, setExpiresAt] = useState(defaultExpiry);
  const [reason, setReason] = useState("");
  const [issuedCode, setIssuedCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<AdminInvitation | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await adminPlatformApi.listInvitations());
    } catch (error) {
      toast.error("邀请列表加载失败", { body: getSafeApiError(error).message });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const normalizedPhone = useMemo(
    () => phone.replace(/\D/g, "").slice(0, 11),
    [phone],
  );

  const create = async () => {
    if (normalizedPhone.length !== 11 || !reason.trim()) {
      toast.error("请填写 11 位手机号和邀请原因");
      return;
    }
    setBusy("create");
    setIssuedCode(null);
    try {
      const created = await adminPlatformApi.createInvitation(
        `+86${normalizedPhone}`,
        new Date(expiresAt).toISOString(),
        reason.trim(),
      );
      setIssuedCode(created.invitation_code || null);
      setPhone("");
      setReason("");
      toast.success("注册邀请已创建");
      await load();
    } catch (error) {
      toast.error("邀请创建失败", { body: getSafeApiError(error).message });
    } finally {
      setBusy(null);
    }
  };

  const revoke = async (revokeReason: string) => {
    if (!revokeTarget) return;
    setBusy(revokeTarget.id);
    try {
      await adminPlatformApi.revokeInvitation(revokeTarget.id, revokeReason);
      toast.success("邀请已撤销");
      setRevokeTarget(null);
      await load();
    } catch (error) {
      toast.error("邀请撤销失败", { body: getSafeApiError(error).message });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-glass-border pb-5">
        <div>
          <p className="text-xs text-primary">平台管理</p>
          <h2 className="mt-1 text-xl font-semibold">注册邀请</h2>
          <p className="mt-1 text-sm text-text-secondary">签发手机号绑定的一次性内测邀请</p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading} title="刷新" aria-label="刷新" className="grid h-9 w-9 place-items-center rounded-md border border-glass-border bg-glass text-text-secondary hover:bg-hover-bg disabled:opacity-50">
          <RefreshCw size={16} className={loading ? "animate-spin" : ""} />
        </button>
      </header>

      <section className="grid gap-3 border-b border-glass-border pb-6 md:grid-cols-[1fr_1fr_1.4fr_auto] md:items-end">
        <label className="text-sm">
          <span className="mb-2 block text-text-secondary">手机号</span>
          <input value={normalizedPhone} onChange={(event) => setPhone(event.target.value)} placeholder="11 位手机号" className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 outline-none focus:border-primary/60" />
        </label>
        <label className="text-sm">
          <span className="mb-2 block text-text-secondary">有效期至</span>
          <input type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 outline-none focus:border-primary/60" />
        </label>
        <label className="text-sm">
          <span className="mb-2 block text-text-secondary">邀请原因</span>
          <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="填写内测邀请原因" className="h-10 w-full rounded-md border border-glass-border bg-glass px-3 outline-none focus:border-primary/60" />
        </label>
        <button type="button" onClick={() => void create()} disabled={busy === "create"} className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-on-accent hover:bg-primary-hover disabled:opacity-50">
          {busy === "create" ? <Loader2 size={15} className="animate-spin" /> : <KeyRound size={15} />}
          创建邀请
        </button>
      </section>

      {issuedCode && (
        <section className="border-l-2 border-emerald-400 bg-emerald-400/5 px-4 py-3">
          <p className="text-sm font-medium text-emerald-200">邀请码仅显示本次</p>
          <div className="mt-2 flex gap-2">
            <input readOnly value={issuedCode} className="h-10 min-w-0 flex-1 rounded-md border border-emerald-400/25 bg-background/60 px-3 font-mono text-sm" />
            <button type="button" title="复制邀请码" aria-label="复制邀请码" onClick={() => void navigator.clipboard.writeText(issuedCode)} className="grid h-10 w-10 place-items-center rounded-md border border-emerald-400/25 text-emerald-200 hover:bg-emerald-400/10"><Clipboard size={16} /></button>
          </div>
        </section>
      )}

      {loading ? (
        <div className="grid min-h-44 place-items-center text-sm text-text-secondary"><Loader2 size={17} className="animate-spin" /></div>
      ) : items.length === 0 ? (
        <div className="grid min-h-44 place-items-center border-y border-glass-border text-sm text-text-muted">暂无注册邀请</div>
      ) : (
        <div className="divide-y divide-glass-border border-y border-glass-border">
          {items.map((item) => (
            <article key={item.id} className="grid gap-3 py-4 md:grid-cols-[minmax(180px,1fr)_minmax(180px,1fr)_auto] md:items-center">
              <div>
                <div className="flex items-center gap-2"><span className="font-mono text-sm">{item.phone}</span><span className="rounded border border-glass-border px-2 py-0.5 text-xs text-text-secondary">{STATUS_LABEL[item.status]}</span></div>
                <p className="mt-1 text-xs text-text-muted">{item.issue_reason}</p>
              </div>
              <div className="text-xs text-text-muted"><p>创建于 {dateText(item.created_at)}</p><p className="mt-1">有效期至 {dateText(item.expires_at)}</p></div>
              <button type="button" title="撤销邀请" aria-label="撤销邀请" disabled={item.status !== "active" || busy === item.id} onClick={() => setRevokeTarget(item)} className="grid h-9 w-9 place-items-center rounded-md border border-red-400/25 text-red-200 hover:bg-red-400/10 disabled:opacity-35">{busy === item.id ? <Loader2 size={15} className="animate-spin" /> : <ShieldX size={15} />}</button>
            </article>
          ))}
        </div>
      )}
      {revokeTarget && <AdminReasonDialog title="撤销注册邀请" description={`手机号 ${revokeTarget.phone} 的邀请码将立即失效，且不能再次使用。`} confirmLabel="确认撤销" danger busy={busy === revokeTarget.id} onClose={() => setRevokeTarget(null)} onConfirm={revoke} />}
    </div>
  );
}
