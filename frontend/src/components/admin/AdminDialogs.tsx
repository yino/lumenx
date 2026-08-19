"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Check, Clipboard, Loader2, X } from "lucide-react";

interface AdminReasonDialogProps {
  title: string;
  description: string;
  confirmLabel: string;
  initialReason?: string;
  danger?: boolean;
  busy?: boolean;
  children?: ReactNode;
  onClose: () => void;
  onConfirm: (reason: string) => void | Promise<void>;
}

export function AdminReasonDialog({
  title,
  description,
  confirmLabel,
  initialReason = "",
  danger = false,
  busy = false,
  children,
  onClose,
  onConfirm,
}: AdminReasonDialogProps) {
  const [reason, setReason] = useState(initialReason);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const reasonId = useId();

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialogRef.current?.querySelector<HTMLElement>("textarea")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      previous?.focus();
    };
  }, [busy, onClose]);

  const submit = async () => {
    const normalized = reason.trim();
    if (!/[\u3400-\u9fff]/.test(normalized)) {
      setError("操作原因必须包含中文说明");
      return;
    }
    setError("");
    await onConfirm(normalized);
  };

  return (
    <div className="fixed inset-0 z-[160] grid place-items-center bg-black/75 px-4 py-8 backdrop-blur-sm" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={titleId} className="w-full max-w-lg overflow-hidden rounded-md border border-glass-border bg-elevated shadow-2xl shadow-black/50">
        <header className="flex items-start justify-between gap-4 border-b border-glass-border px-5 py-4">
          <div>
            <h2 id={titleId} className="text-base font-semibold">{title}</h2>
            <p className="mt-1 text-sm leading-6 text-text-secondary">{description}</p>
          </div>
          <button type="button" aria-label="关闭" title="关闭" disabled={busy} onClick={onClose} className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-text-muted hover:bg-hover-bg hover:text-foreground disabled:opacity-40"><X size={17} /></button>
        </header>
        <div className="space-y-4 px-5 py-5">
          {children}
          <label htmlFor={reasonId} className="block text-sm font-medium">操作原因</label>
          <textarea id={reasonId} value={reason} onChange={(event) => setReason(event.target.value)} disabled={busy} rows={3} maxLength={2000} aria-invalid={Boolean(error)} aria-describedby={error ? `${reasonId}-error` : undefined} placeholder="填写中文原因，将写入审计记录" className="w-full resize-none rounded-md border border-glass-border bg-input-bg px-3 py-2 text-sm leading-6 outline-none focus:border-primary/60" />
          {error && <p id={`${reasonId}-error`} role="alert" className="text-sm text-red-300">{error}</p>}
        </div>
        <footer className="flex justify-end gap-2 border-t border-glass-border px-5 py-4">
          <button type="button" disabled={busy} onClick={onClose} className="h-9 rounded-md border border-glass-border bg-glass px-3 text-sm text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-40">取消</button>
          <button type="button" disabled={busy} onClick={() => void submit()} className={`inline-flex h-9 items-center gap-2 rounded-md px-3 text-sm font-medium disabled:opacity-40 ${danger ? "bg-red-500 text-white hover:bg-red-400" : "bg-primary text-on-accent hover:bg-primary-hover"}`}>
            {busy ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}{confirmLabel}
          </button>
        </footer>
      </div>
    </div>
  );
}

export function AdminOneTimeResultDialog({ title, description, value, expiresAt, onClose }: { title: string; description: string; value: string; expiresAt?: string; onClose: () => void }) {
  const titleId = useId();
  const [copied, setCopied] = useState(false);
  return (
    <div className="fixed inset-0 z-[170] grid place-items-center bg-black/75 px-4 py-8 backdrop-blur-sm">
      <section role="dialog" aria-modal="true" aria-labelledby={titleId} className="w-full max-w-lg rounded-md border border-emerald-400/25 bg-elevated shadow-2xl shadow-black/50">
        <header className="flex items-start justify-between gap-4 border-b border-glass-border px-5 py-4"><div><h2 id={titleId} className="text-base font-semibold">{title}</h2><p className="mt-1 text-sm text-text-secondary">{description}</p></div><button type="button" autoFocus aria-label="关闭" title="关闭" onClick={onClose} className="grid h-8 w-8 place-items-center rounded-md text-text-muted hover:bg-hover-bg"><X size={17} /></button></header>
        <div className="px-5 py-5"><div className="flex gap-2"><input readOnly value={value} aria-label="一次性凭据" className="h-11 min-w-0 flex-1 rounded-md border border-emerald-400/25 bg-background px-3 font-mono text-sm" /><button type="button" title="复制" aria-label="复制" onClick={async () => { await navigator.clipboard.writeText(value); setCopied(true); }} className="grid h-11 w-11 place-items-center rounded-md border border-emerald-400/25 text-emerald-200 hover:bg-emerald-400/10">{copied ? <Check size={16} /> : <Clipboard size={16} />}</button></div>{expiresAt && <p className="mt-3 text-xs text-text-muted">有效期至 {new Date(expiresAt).toLocaleString("zh-CN")}</p>}</div>
        <footer className="flex justify-end border-t border-glass-border px-5 py-4"><button type="button" onClick={onClose} className="h-9 rounded-md bg-primary px-4 text-sm font-medium text-on-accent hover:bg-primary-hover">已妥善保存</button></footer>
      </section>
    </div>
  );
}
