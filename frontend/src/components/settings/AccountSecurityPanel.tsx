"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  KeyRound,
  Loader2,
  LogOut,
  MonitorSmartphone,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from "lucide-react";

import { authApi, getSafeApiError, type AuthSession } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { toast } from "@/store/toastStore";

function formatPhone(phone: string): string {
  const matched = phone.match(/^(\+86)(\d{3})(\d{4})(\d{4})$/);
  return matched ? `${matched[1]} ${matched[2]} **** ${matched[4]}` : phone;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : date.toLocaleString("zh-CN", { hour12: false });
}

function validatePassword(password: string, confirmation: string): string | null {
  if (password.length < 10) return "新密码至少需要 10 位";
  if (!/[A-Za-z]/.test(password)) return "新密码至少需要包含一个字母";
  if (!/\d/.test(password)) return "新密码至少需要包含一个数字";
  if (password !== confirmation) return "两次输入的新密码不一致";
  return null;
}

export default function AccountSecurityPanel() {
  const user = useAuthStore((state) => state.user);
  const setAnonymous = useAuthStore((state) => state.setAnonymous);
  const [sessions, setSessions] = useState<AuthSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [passwordSaving, setPasswordSaving] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [confirmRevokeAll, setConfirmRevokeAll] = useState(false);
  const [logoutPending, setLogoutPending] = useState(false);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      setSessions(await authApi.listSessions());
    } catch (error) {
      toast.error("无法加载登录设备", { body: getSafeApiError(error).message });
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const changePassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPasswordError(null);
    if (!currentPassword) {
      setPasswordError("请输入当前密码");
      return;
    }
    const validationError = validatePassword(newPassword, confirmation);
    if (validationError) {
      setPasswordError(validationError);
      return;
    }
    setPasswordSaving(true);
    try {
      const result = await authApi.changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmation("");
      toast.success(result.message);
      await loadSessions();
    } catch (error) {
      setPasswordError(getSafeApiError(error).message);
    } finally {
      setPasswordSaving(false);
    }
  };

  const revokeSession = async (sessionId: string) => {
    try {
      const result = await authApi.revokeSession(sessionId);
      toast.success(result.message);
      await loadSessions();
    } catch (error) {
      toast.error("无法撤销该设备", { body: getSafeApiError(error).message });
    }
  };

  const logoutCurrent = async () => {
    setLogoutPending(true);
    try {
      await authApi.logout();
      setAnonymous(null);
      window.location.hash = "#/";
    } catch (error) {
      toast.error("退出失败", { body: getSafeApiError(error).message });
    } finally {
      setLogoutPending(false);
    }
  };

  const revokeAll = async () => {
    setLogoutPending(true);
    try {
      await authApi.revokeAllSessions();
      setAnonymous("所有设备均已退出，请重新登录");
      window.location.hash = "#/";
    } catch (error) {
      toast.error("无法退出所有设备", { body: getSafeApiError(error).message });
    } finally {
      setLogoutPending(false);
      setConfirmRevokeAll(false);
    }
  };

  if (!user) return null;

  return (
    <div className="space-y-6">
      <section className="glass-panel atelier-card overflow-hidden rounded-lg" aria-labelledby="account-profile-title">
        <header className="border-b border-glass-border px-5 py-4">
          <h2 id="account-profile-title" className="font-display text-lg font-semibold text-foreground">
            账号信息
          </h2>
        </header>
        <div className="flex flex-col gap-4 px-5 py-5 sm:flex-row sm:items-center">
          <div className="grid h-11 w-11 flex-none place-items-center rounded-full bg-primary/10 text-primary">
            <ShieldCheck size={21} />
          </div>
          <div className="min-w-0 flex-1">
            <p className="font-mono text-sm text-foreground">{formatPhone(user.phone)}</p>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-text-secondary">
              <span>{user.phone_verification_status}</span>
              {user.is_platform_admin && (
                <span className="rounded bg-accent/10 px-1.5 py-0.5 text-accent">平台管理员</span>
              )}
            </div>
          </div>
          <button
            type="button"
            onClick={logoutCurrent}
            disabled={logoutPending}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-lg border border-glass-border px-3 text-sm text-text-secondary hover:bg-hover-bg hover:text-foreground disabled:opacity-50"
          >
            {logoutPending ? <Loader2 size={15} className="animate-spin" /> : <LogOut size={15} />}
            退出当前账号
          </button>
        </div>
      </section>

      <section className="glass-panel atelier-card overflow-hidden rounded-lg" aria-labelledby="password-title">
        <header className="border-b border-glass-border px-5 py-4">
          <div className="flex items-center gap-2">
            <KeyRound size={17} className="text-primary" />
            <h2 id="password-title" className="font-display text-lg font-semibold text-foreground">
              修改密码
            </h2>
          </div>
          <p className="mt-1 text-xs text-text-secondary">更新后，其他设备上的登录会话将失效。</p>
        </header>
        <form className="grid gap-4 px-5 py-5 md:grid-cols-3" onSubmit={changePassword}>
          <label className="text-sm font-medium text-foreground">
            当前密码
            <input
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
              className="glass-input mt-2 h-11 w-full"
              disabled={passwordSaving}
            />
          </label>
          <label className="text-sm font-medium text-foreground">
            新密码
            <input
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              placeholder="至少 10 位，包含字母和数字"
              className="glass-input mt-2 h-11 w-full"
              disabled={passwordSaving}
            />
          </label>
          <label className="text-sm font-medium text-foreground">
            确认新密码
            <input
              type="password"
              autoComplete="new-password"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              className="glass-input mt-2 h-11 w-full"
              disabled={passwordSaving}
            />
          </label>
          {passwordError && (
            <p role="alert" className="text-sm text-status-failed-fg md:col-span-2">
              {passwordError}
            </p>
          )}
          <div className="flex justify-end md:col-span-3">
            <button
              type="submit"
              disabled={passwordSaving}
              className="inline-flex h-10 items-center gap-2 rounded-lg bg-primary px-4 text-sm font-semibold text-on-accent hover:bg-primary-hover disabled:opacity-50"
            >
              {passwordSaving && <Loader2 size={15} className="animate-spin" />}
              更新密码
            </button>
          </div>
        </form>
      </section>

      <section className="glass-panel atelier-card overflow-hidden rounded-lg" aria-labelledby="sessions-title">
        <header className="flex items-center justify-between gap-3 border-b border-glass-border px-5 py-4">
          <div>
            <div className="flex items-center gap-2">
              <MonitorSmartphone size={17} className="text-primary" />
              <h2 id="sessions-title" className="font-display text-lg font-semibold text-foreground">
                登录设备
              </h2>
            </div>
            <p className="mt-1 text-xs text-text-secondary">撤销不再使用的设备会话。</p>
          </div>
          <button
            type="button"
            onClick={() => void loadSessions()}
            disabled={sessionsLoading}
            className="grid h-9 w-9 place-items-center rounded-lg text-text-muted hover:bg-hover-bg hover:text-foreground disabled:opacity-50"
            aria-label="刷新登录设备"
            title="刷新登录设备"
          >
            <RefreshCw size={16} className={sessionsLoading ? "animate-spin" : ""} />
          </button>
        </header>
        <div className="divide-y divide-glass-border px-5">
          {!sessionsLoading && sessions.length === 0 && (
            <p className="py-6 text-sm text-text-muted">暂无有效登录设备</p>
          )}
          {sessions.map((session) => (
            <div key={session.id} className="flex items-center gap-3 py-4">
              <MonitorSmartphone size={17} className="flex-none text-text-muted" />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm text-foreground">
                    {session.user_agent || "未知设备"}
                  </span>
                  {session.current && (
                    <span className="flex-none rounded bg-primary/10 px-1.5 py-0.5 text-[0.625rem] text-primary">
                      当前设备
                    </span>
                  )}
                </div>
                <p className="mt-1 text-xs text-text-muted">
                  最近使用 {formatDate(session.last_seen_at)} · 最晚失效 {formatDate(session.absolute_expires_at)}
                </p>
              </div>
              {!session.current && (
                <button
                  type="button"
                  onClick={() => void revokeSession(session.id)}
                  className="grid h-9 w-9 flex-none place-items-center rounded-lg text-text-muted hover:bg-status-failed-bg hover:text-status-failed-fg"
                  aria-label="撤销该设备"
                  title="撤销该设备"
                >
                  <Trash2 size={15} />
                </button>
              )}
            </div>
          ))}
        </div>
        <footer className="flex justify-end border-t border-glass-border px-5 py-4">
          {confirmRevokeAll ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-status-failed-fg">确认退出所有设备？</span>
              <button
                type="button"
                onClick={() => setConfirmRevokeAll(false)}
                className="h-9 rounded-lg border border-glass-border px-3 text-sm text-text-secondary hover:text-foreground"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => void revokeAll()}
                disabled={logoutPending}
                className="h-9 rounded-lg bg-status-failed-bg px-3 text-sm font-semibold text-status-failed-fg disabled:opacity-50"
              >
                确认退出
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmRevokeAll(true)}
              className="inline-flex h-9 items-center gap-2 rounded-lg border border-status-failed-border px-3 text-sm text-status-failed-fg hover:bg-status-failed-bg"
            >
              <LogOut size={15} />
              退出所有设备
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}
