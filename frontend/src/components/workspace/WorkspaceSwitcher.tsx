"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import {
  ArchiveRestore,
  Check,
  ChevronDown,
  Loader2,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";

import { getSafeApiError } from "@/lib/api";
import { useWorkspaceStore } from "@/store/workspaceStore";
import { toast } from "@/store/toastStore";

function restoreDeadline(value: string | null): string {
  if (!value) return "恢复期限未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "恢复期限未知"
    : `${date.toLocaleDateString("zh-CN")} 前可恢复`;
}

export default function WorkspaceSwitcher({ compact = false }: { compact?: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const workspaces = useWorkspaceStore((state) => state.workspaces);
  const deletedWorkspaces = useWorkspaceStore((state) => state.deletedWorkspaces);
  const currentWorkspaceId = useWorkspaceStore((state) => state.currentWorkspaceId);
  const busyWorkspaceId = useWorkspaceStore((state) => state.busyWorkspaceId);
  const createWorkspace = useWorkspaceStore((state) => state.createWorkspace);
  const switchWorkspace = useWorkspaceStore((state) => state.switchWorkspace);
  const renameWorkspace = useWorkspaceStore((state) => state.renameWorkspace);
  const deleteWorkspace = useWorkspaceStore((state) => state.deleteWorkspace);
  const restoreWorkspace = useWorkspaceStore((state) => state.restoreWorkspace);
  const [open, setOpen] = useState(false);
  const [showDeleted, setShowDeleted] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const current = workspaces.find((workspace) => workspace.id === currentWorkspaceId);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const run = async (action: () => Promise<unknown>, success?: string) => {
    try {
      await action();
      if (success) toast.success(success);
    } catch (error) {
      toast.error("工作区操作失败", { body: getSafeApiError(error).message });
    }
  };

  const submitCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    await run(async () => {
      await createWorkspace(name);
      setNewName("");
      setCreating(false);
      setOpen(false);
      window.location.hash = "#/";
    }, "工作区已创建");
  };

  const submitRename = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editingId || !editingName.trim()) return;
    await run(async () => {
      await renameWorkspace(editingId, editingName);
      setEditingId(null);
    }, "工作区已重命名");
  };

  return (
    <div ref={containerRef} className={`relative ${compact ? "w-full" : "px-2.5 py-2"}`}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={`flex w-full items-center gap-2 rounded-lg border border-glass-border bg-glass text-left hover:bg-hover-bg ${compact ? "h-10 px-3" : "px-3 py-2.5"}`}
      >
        <span className="grid h-7 w-7 flex-none place-items-center rounded-md bg-primary/10 font-mono text-xs font-semibold text-primary">
          {current?.name.slice(0, 1) || "工"}
        </span>
        <span className="min-w-0 flex-1">
          {!compact && <span className="block text-[0.625rem] text-text-muted">当前工作区</span>}
          <span className="block truncate text-sm font-semibold text-foreground">
            {current?.name || "选择工作区"}
          </span>
        </span>
        <ChevronDown size={15} className={`flex-none text-text-muted transition-transform ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <section className={`absolute z-50 overflow-hidden rounded-lg border border-glass-border bg-elevated shadow-2xl ${compact ? "left-0 right-0 top-full mt-2" : "left-2.5 right-2.5 top-full"}`} aria-label="工作区管理">
          <div className="flex items-center justify-between border-b border-glass-border px-3 py-2.5">
            <span className="text-xs font-semibold text-text-secondary">我的工作区</span>
            <button type="button" onClick={() => setCreating(true)} className="grid h-7 w-7 place-items-center rounded-md text-text-muted hover:bg-hover-bg hover:text-foreground" aria-label="新建工作区" title="新建工作区">
              <Plus size={15} />
            </button>
          </div>

          <div className="max-h-72 overflow-y-auto p-1.5">
            {creating && (
              <form onSubmit={submitCreate} className="mb-1 flex items-center gap-1.5 rounded-md bg-surface-inset p-1.5">
                <input autoFocus value={newName} onChange={(event) => setNewName(event.target.value)} maxLength={120} placeholder="工作区名称" className="min-w-0 flex-1 bg-transparent px-1.5 text-sm text-foreground outline-none placeholder:text-text-muted" />
                <button type="submit" disabled={!newName.trim() || busyWorkspaceId === "new"} className="grid h-7 w-7 place-items-center rounded-md text-primary hover:bg-primary/10 disabled:opacity-40" aria-label="确认创建">
                  {busyWorkspaceId === "new" ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
                </button>
                <button type="button" onClick={() => { setCreating(false); setNewName(""); }} className="grid h-7 w-7 place-items-center rounded-md text-text-muted hover:bg-hover-bg" aria-label="取消创建"><X size={14} /></button>
              </form>
            )}

            {workspaces.map((workspace) => {
              const active = workspace.id === currentWorkspaceId;
              const busy = workspace.id === busyWorkspaceId;
              if (editingId === workspace.id) {
                return (
                  <form key={workspace.id} onSubmit={submitRename} className="mb-1 flex items-center gap-1.5 rounded-md bg-surface-inset p-1.5">
                    <input autoFocus value={editingName} onChange={(event) => setEditingName(event.target.value)} maxLength={120} className="min-w-0 flex-1 bg-transparent px-1.5 text-sm text-foreground outline-none" />
                    <button type="submit" disabled={!editingName.trim() || busy} className="grid h-7 w-7 place-items-center rounded-md text-primary hover:bg-primary/10 disabled:opacity-40" aria-label="保存名称">{busy ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}</button>
                    <button type="button" onClick={() => setEditingId(null)} className="grid h-7 w-7 place-items-center rounded-md text-text-muted hover:bg-hover-bg" aria-label="取消重命名"><X size={14} /></button>
                  </form>
                );
              }
              return (
                <div key={workspace.id} className={`mb-1 rounded-md ${active ? "bg-primary/10" : "hover:bg-hover-bg"}`}>
                  {confirmDeleteId === workspace.id ? (
                    <div className="flex items-center gap-2 px-2 py-2 text-xs">
                      <span className="min-w-0 flex-1 text-text-secondary">移入回收站？</span>
                      <button type="button" disabled={busy} onClick={() => void run(async () => { await deleteWorkspace(workspace.id); setConfirmDeleteId(null); window.location.hash = "#/"; }, "工作区已移入回收站")} className="rounded px-2 py-1 text-status-failed-fg hover:bg-status-failed-bg">确认</button>
                      <button type="button" onClick={() => setConfirmDeleteId(null)} className="rounded px-2 py-1 text-text-muted hover:bg-hover-bg">取消</button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-1 px-1 py-1">
                      <button type="button" disabled={busy} onClick={() => void run(async () => { await switchWorkspace(workspace.id); setOpen(false); window.location.hash = "#/"; })} className="flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1.5 text-left disabled:opacity-50">
                        <span className={`grid h-5 w-5 flex-none place-items-center ${active ? "text-primary" : "text-transparent"}`}>{busy ? <Loader2 size={13} className="animate-spin text-text-muted" /> : <Check size={13} />}</span>
                        <span className="truncate text-sm text-foreground">{workspace.name}</span>
                      </button>
                      <button type="button" onClick={() => { setEditingId(workspace.id); setEditingName(workspace.name); }} className="grid h-7 w-7 flex-none place-items-center rounded text-text-muted hover:bg-surface hover:text-foreground" aria-label={`重命名${workspace.name}`} title="重命名"><Pencil size={13} /></button>
                      <button type="button" onClick={() => setConfirmDeleteId(workspace.id)} className="grid h-7 w-7 flex-none place-items-center rounded text-text-muted hover:bg-status-failed-bg hover:text-status-failed-fg" aria-label={`删除${workspace.name}`} title="移入回收站"><Trash2 size={13} /></button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <div className="border-t border-glass-border p-1.5">
            <button type="button" onClick={() => setShowDeleted((value) => !value)} className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-xs text-text-muted hover:bg-hover-bg hover:text-foreground">
              <ArchiveRestore size={14} />
              <span className="flex-1 text-left">回收站</span>
              <span className="font-mono">{deletedWorkspaces.length}</span>
            </button>
            {showDeleted && (
              <div className="max-h-40 overflow-y-auto border-t border-glass-border pt-1.5">
                {deletedWorkspaces.length === 0 ? (
                  <p className="px-2.5 py-3 text-xs text-text-muted">回收站为空</p>
                ) : deletedWorkspaces.map((workspace) => (
                  <div key={workspace.id} className="flex items-center gap-2 rounded-md px-2.5 py-2 hover:bg-hover-bg">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs text-text-secondary">{workspace.name}</p>
                      <p className="mt-0.5 text-[0.625rem] text-text-muted">{restoreDeadline(workspace.retention_expires_at)}</p>
                    </div>
                    <button type="button" disabled={busyWorkspaceId === workspace.id} onClick={() => void run(() => restoreWorkspace(workspace.id), "工作区已恢复")} className="grid h-7 w-7 place-items-center rounded text-primary hover:bg-primary/10 disabled:opacity-40" aria-label={`恢复${workspace.name}`} title="恢复工作区">
                      {busyWorkspaceId === workspace.id ? <Loader2 size={13} className="animate-spin" /> : <ArchiveRestore size={13} />}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
