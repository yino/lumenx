"use client";

import Image from "next/image";
import { useEffect, useId, useState, type FormEvent, type ReactNode } from "react";
import { ArrowRight, KeyRound, Loader2, LockKeyhole, LogOut, ShieldCheck, UserRound } from "lucide-react";

import {
  ADMIN_SESSION_EXPIRED_EVENT,
  adminAuthApi,
  getSafeApiError,
  type AdminAuthUser,
  type SafeAPIError,
} from "@/lib/api";
import { useAdminAuthStore } from "@/store/adminAuthStore";

function AdminLoginScreen({
  initialError,
  onAuthenticated,
}: {
  initialError: string | null;
  onAuthenticated: (admin: AdminAuthUser) => void;
}) {
  const usernameId = useId();
  const passwordId = useId();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(initialError);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError("请输入管理员账号和密码");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const response = await adminAuthApi.login(username.trim(), password);
      onAuthenticated(response.admin);
    } catch (caught) {
      setError(getSafeApiError(caught).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="min-h-screen bg-background px-5 py-8 text-foreground">
      <div className="mx-auto flex min-h-[calc(100vh-4rem)] w-full max-w-[1120px] flex-col">
        <header className="flex items-center justify-between border-b border-glass-border pb-5">
          <div className="flex items-center gap-3">
            <Image src="/logo-dark.png" alt="LumenX" width={38} height={38} className="h-9 w-9 object-contain" />
            <div>
              <p className="font-display text-base font-semibold">LumenX 系统后台</p>
              <p className="text-xs text-text-muted">独立管理员身份域</p>
            </div>
          </div>
          <div className="hidden items-center gap-2 text-xs text-emerald-200 sm:flex">
            <span className="h-2 w-2 rounded-full bg-emerald-300" />
            管理入口已隔离
          </div>
        </header>

        <div className="grid flex-1 items-center gap-10 py-10 lg:grid-cols-[minmax(0,1fr)_420px]">
          <section className="max-w-xl">
            <p className="font-mono text-xs text-primary">系统控制台</p>
            <h1 className="mt-3 font-display text-3xl font-semibold leading-tight sm:text-4xl">系统运营与资产审查</h1>
            <p className="mt-4 max-w-lg text-sm leading-7 text-text-secondary">
              管理员账号与创作用户完全分离。此入口仅用于用户管理、充值订单、平台场景、AI 任务和审计操作。
            </p>
            <div className="mt-8 grid gap-px overflow-hidden rounded-md border border-glass-border bg-glass-border sm:grid-cols-3">
              {[
                ["身份", "独立会话"],
                ["范围", "系统控制面"],
                ["记录", "全程审计"],
              ].map(([label, value]) => (
                <div key={label} className="bg-background/90 px-4 py-4">
                  <span className="block text-xs text-text-muted">{label}</span>
                  <strong className="mt-1 block text-sm font-medium">{value}</strong>
                </div>
              ))}
            </div>
          </section>

          <form onSubmit={submit} className="rounded-md border border-glass-border bg-surface/55 p-6 shadow-2xl backdrop-blur-xl sm:p-8">
            <div className="flex items-start gap-3">
              <span className="grid h-10 w-10 shrink-0 place-items-center rounded-md bg-primary/12 text-primary"><ShieldCheck size={20} /></span>
              <div>
                <h2 className="text-lg font-semibold">管理员登录</h2>
                <p className="mt-1 text-xs text-text-muted">使用系统管理员凭据，不接受手机号用户账号</p>
              </div>
            </div>

            <div className="mt-7 space-y-5">
              <div>
                <label htmlFor={usernameId} className="mb-2 block text-sm font-medium">管理员账号</label>
                <div className="relative">
                  <UserRound size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
                  <input id={usernameId} value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" maxLength={32} className="h-11 w-full rounded-md border border-glass-border bg-input-bg pl-10 pr-3 text-sm outline-none focus:border-primary/70" />
                </div>
              </div>
              <div>
                <label htmlFor={passwordId} className="mb-2 block text-sm font-medium">管理员密码</label>
                <div className="relative">
                  <LockKeyhole size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
                  <input id={passwordId} type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" maxLength={128} className="h-11 w-full rounded-md border border-glass-border bg-input-bg pl-10 pr-3 text-sm outline-none focus:border-primary/70" autoFocus />
                </div>
              </div>
            </div>

            {error && <p role="alert" className="mt-4 rounded-md border border-red-400/25 bg-red-400/10 px-3 py-2 text-sm text-red-200">{error}</p>}

            <button type="submit" disabled={submitting} className="mt-6 inline-flex h-11 w-full items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-white transition-colors hover:bg-primary/90 disabled:opacity-60">
              {submitting ? <Loader2 size={17} className="animate-spin" /> : <ArrowRight size={17} />}
              {submitting ? "正在验证" : "进入系统后台"}
            </button>
          </form>
        </div>
      </div>
    </main>
  );
}

function AdminPasswordChangeScreen({
  admin,
  onChanged,
  onLogout,
}: {
  admin: AdminAuthUser;
  onChanged: (admin: AdminAuthUser) => void;
  onLogout: () => void;
}) {
  const currentPasswordId = useId();
  const newPasswordId = useId();
  const confirmPasswordId = useId();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (newPassword.length < 10) {
      setError("新密码至少需要 10 位");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await adminAuthApi.changePassword(currentPassword, newPassword);
      onChanged({ ...admin, must_change_password: false });
    } catch (caught) {
      setError(getSafeApiError(caught).message);
    } finally {
      setSubmitting(false);
    }
  };

  const logout = async () => {
    setLoggingOut(true);
    try {
      await adminAuthApi.logout();
    } finally {
      onLogout();
    }
  };

  return (
    <main className="grid min-h-screen place-items-center bg-background px-5 py-8 text-foreground">
      <form onSubmit={submit} className="w-full max-w-md rounded-md border border-glass-border bg-surface/55 p-6 shadow-2xl backdrop-blur-xl sm:p-8">
        <div className="flex items-start gap-3">
          <span className="grid h-10 w-10 shrink-0 place-items-center rounded-md bg-primary/12 text-primary"><KeyRound size={20} /></span>
          <div>
            <h1 className="text-lg font-semibold">修改初始管理员密码</h1>
            <p className="mt-1 text-sm leading-6 text-text-secondary">完成密码修改后才能进入系统后台。</p>
          </div>
        </div>
        <div className="mt-6 space-y-4">
          <div><label htmlFor={currentPasswordId} className="mb-2 block text-sm font-medium">当前密码</label><input id={currentPasswordId} type="password" autoComplete="current-password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} className="h-11 w-full rounded-md border border-glass-border bg-input-bg px-3 text-sm outline-none focus:border-primary/70" autoFocus /></div>
          <div><label htmlFor={newPasswordId} className="mb-2 block text-sm font-medium">新密码</label><input id={newPasswordId} type="password" autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} className="h-11 w-full rounded-md border border-glass-border bg-input-bg px-3 text-sm outline-none focus:border-primary/70" /></div>
          <div><label htmlFor={confirmPasswordId} className="mb-2 block text-sm font-medium">确认新密码</label><input id={confirmPasswordId} type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} className="h-11 w-full rounded-md border border-glass-border bg-input-bg px-3 text-sm outline-none focus:border-primary/70" /></div>
        </div>
        {error && <p role="alert" className="mt-4 rounded-md border border-red-400/25 bg-red-400/10 px-3 py-2 text-sm text-red-200">{error}</p>}
        <button type="submit" disabled={submitting || loggingOut} className="mt-6 inline-flex h-11 w-full items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-semibold text-white disabled:opacity-60">{submitting ? <Loader2 size={17} className="animate-spin" /> : <ArrowRight size={17} />}{submitting ? "正在修改" : "修改并进入后台"}</button>
        <button type="button" disabled={submitting || loggingOut} onClick={() => void logout()} className="mt-3 inline-flex h-10 w-full items-center justify-center gap-2 text-sm text-text-secondary hover:text-foreground disabled:opacity-60">{loggingOut ? <Loader2 size={16} className="animate-spin" /> : <LogOut size={16} />}退出登录</button>
      </form>
    </main>
  );
}

export default function AdminAuthGate({ children }: { children: ReactNode }) {
  const status = useAdminAuthStore((state) => state.status);
  const error = useAdminAuthStore((state) => state.error);
  const admin = useAdminAuthStore((state) => state.admin);
  const beginSessionCheck = useAdminAuthStore((state) => state.beginSessionCheck);
  const setAuthenticated = useAdminAuthStore((state) => state.setAuthenticated);
  const setAnonymous = useAdminAuthStore((state) => state.setAnonymous);

  useEffect(() => {
    beginSessionCheck();
    let active = true;
    const handleSessionExpired = (event: Event) => {
      const detail = (event as CustomEvent<SafeAPIError>).detail;
      setAnonymous(detail?.message || "管理员登录状态已失效，请重新登录");
    };
    window.addEventListener(ADMIN_SESSION_EXPIRED_EVENT, handleSessionExpired);
    adminAuthApi.currentAdmin()
      .then(({ admin }) => { if (active) setAuthenticated(admin); })
      .catch(() => { if (active) setAnonymous(); });
    return () => {
      active = false;
      window.removeEventListener(ADMIN_SESSION_EXPIRED_EVENT, handleSessionExpired);
    };
  }, [beginSessionCheck, setAnonymous, setAuthenticated]);

  if (status === "checking") {
    return <main className="grid min-h-screen place-items-center bg-background text-foreground"><div className="flex items-center gap-3 text-sm text-text-secondary" role="status"><Loader2 size={18} className="animate-spin text-primary" />正在验证管理员会话</div></main>;
  }
  if (status === "anonymous") {
    return <AdminLoginScreen initialError={error} onAuthenticated={setAuthenticated} />;
  }
  if (admin?.must_change_password) {
    return (
      <AdminPasswordChangeScreen
        admin={admin}
        onChanged={setAuthenticated}
        onLogout={() => setAnonymous()}
      />
    );
  }
  return <>{children}</>;
}
