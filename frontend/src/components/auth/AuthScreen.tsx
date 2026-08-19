"use client";

import { FormEvent, useEffect, useId, useMemo, useState } from "react";
import { Eye, EyeOff, KeyRound, Loader2, LockKeyhole, Smartphone, UserRound } from "lucide-react";
import Image from "next/image";

import {
  authApi,
  getSafeAuthError,
  type AuthUser,
  type RegistrationPolicy,
} from "@/lib/api";

type AuthMode = "login" | "register";

interface AuthScreenProps {
  initialMode?: AuthMode;
  initialError?: string | null;
  onAuthenticated: (user: AuthUser) => void;
}

const AUTH_IMAGES = [
  {
    src: "/assets/styles/live_action__hong_kong_cinema__cha_chaan_teng_night__landscape.png",
    alt: "港风夜景样片",
    label: "镜头一",
  },
  {
    src: "/assets/styles/style_lab__chinese_ink_fantasy__swordswoman_mountain_v2__portrait.png",
    alt: "水墨幻想人物样片",
    label: "镜头二",
  },
  {
    src: "/assets/styles/japanese_anime__modern_cel_anime__rooftop_sunset_v2__landscape.png",
    alt: "天台落日动画样片",
    label: "镜头三",
  },
] as const;

function validateRegistration(password: string, confirmation: string): string | null {
  if (password.length < 10) return "密码至少需要 10 位";
  if (!/[A-Za-z]/.test(password)) return "密码至少需要包含一个字母";
  if (!/\d/.test(password)) return "密码至少需要包含一个数字";
  if (password !== confirmation) return "两次输入的密码不一致";
  return null;
}

export default function AuthScreen({
  initialMode = "login",
  initialError = null,
  onAuthenticated,
}: AuthScreenProps) {
  const [mode, setMode] = useState<AuthMode>(initialMode);
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [invitationCode, setInvitationCode] = useState("");
  const [registrationPolicy, setRegistrationPolicy] = useState<RegistrationPolicy | null>(null);
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(initialError);
  const phoneId = useId();
  const passwordId = useId();
  const confirmationId = useId();
  const invitationId = useId();

  const normalizedPhone = useMemo(() => identifier.replace(/\D/g, "").slice(0, 11), [identifier]);
  const normalizedLoginIdentifier = useMemo(() => identifier.trim().slice(0, 40), [identifier]);
  const canRegister = registrationPolicy?.mode !== "disabled" && registrationPolicy !== null;
  const inviteOnly = registrationPolicy?.mode === "invite_only";

  useEffect(() => {
    let active = true;
    authApi.registrationPolicy()
      .then((policy) => {
        if (!active) return;
        setRegistrationPolicy(policy);
        if (policy.mode === "disabled") setMode("login");
      })
      .catch(() => {
        if (!active) return;
        setRegistrationPolicy({ mode: "disabled", verification_available: false });
        setMode("login");
      });
    return () => { active = false; };
  }, []);

  const switchMode = (nextMode: AuthMode) => {
    setMode(nextMode);
    setError(null);
    setPassword("");
    setConfirmation("");
    setInvitationCode("");
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    if (mode === "register" && normalizedPhone.length !== 11) {
      setError("请输入 11 位中国大陆手机号");
      return;
    }
    if (mode === "login" && !normalizedLoginIdentifier) {
      setError("请输入手机号或用户名");
      return;
    }
    if (!password) {
      setError("请输入密码");
      return;
    }
    if (mode === "register") {
      if (!canRegister) {
        setError("注册暂未开放，请稍后再试");
        return;
      }
      if (inviteOnly && !invitationCode.trim()) {
        setError("请输入邀请码");
        return;
      }
      const validationError = validateRegistration(password, confirmation);
      if (validationError) {
        setError(validationError);
        return;
      }
    }

    setSubmitting(true);
    try {
      const response =
        mode === "login"
          ? await authApi.login(normalizedLoginIdentifier, password)
          : await authApi.register(`+86${normalizedPhone}`, password, invitationCode.trim());
      onAuthenticated(response.user);
    } catch (requestError) {
      setError(getSafeAuthError(requestError).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="min-h-screen bg-background text-foreground lg:grid lg:grid-cols-[minmax(420px,0.88fr)_minmax(560px,1.12fr)]">
      <section className="relative z-10 flex min-h-screen flex-col border-b border-glass-border bg-background/95 px-6 py-6 backdrop-blur-xl sm:px-10 lg:border-b-0 lg:border-r lg:px-[clamp(3rem,6vw,6.5rem)] lg:py-9">
        <div className="flex items-center gap-3" aria-label="LumenX 创作台">
          <Image
            src="/logo-dark.png"
            alt="LumenX"
            width={44}
            height={44}
            className="h-11 w-11 object-contain [filter:hue-rotate(-64deg)_saturate(1.35)_brightness(1.08)]"
          />
          <div>
            <div className="font-mono text-lg font-bold leading-none text-foreground">
              LUMEN<span className="text-primary">X</span>
            </div>
            <div className="mt-1 text-xs text-text-muted">创作台</div>
          </div>
        </div>

        <div className="my-auto w-full max-w-[420px] py-12 lg:py-16">
          <p className="mb-3 text-xs font-semibold text-primary">
            {mode === "login" ? "继续创作" : "建立创作档案"}
          </p>
          <h1 className="font-display text-4xl font-semibold leading-[1.08] text-foreground lg:text-5xl">
            {mode === "login" ? "欢迎回来" : "让故事从这里开场"}
          </h1>
          <p className="mt-4 text-sm leading-6 text-text-secondary">
            {mode === "login" ? "登录后继续你的项目。" : "注册后将自动创建默认工作区。"}
          </p>

          <div
            className={`mt-8 grid h-11 rounded-lg border border-glass-border bg-surface-inset p-1 ${canRegister ? "grid-cols-2" : "grid-cols-1"}`}
            role="tablist"
            aria-label="登录或注册"
          >
            <button
              type="button"
              role="tab"
              aria-selected={mode === "login"}
              onClick={() => switchMode("login")}
              className={`rounded-md text-sm font-semibold transition-colors ${
                mode === "login"
                  ? "bg-surface text-foreground shadow-sm"
                  : "text-text-muted hover:text-foreground"
              }`}
            >
              登录
            </button>
            {canRegister && (
              <button
                type="button"
                role="tab"
                aria-selected={mode === "register"}
                onClick={() => switchMode("register")}
                className={`rounded-md text-sm font-semibold transition-colors ${
                  mode === "register"
                    ? "bg-surface text-foreground shadow-sm"
                    : "text-text-muted hover:text-foreground"
                }`}
              >
                注册
              </button>
            )}
          </div>

          <form className="mt-7 space-y-5" onSubmit={handleSubmit} noValidate>
            <div>
              <label htmlFor={phoneId} className="mb-2 block text-sm font-medium text-foreground">
                {mode === "login" ? "手机号或用户名" : "手机号"}
              </label>
              <div className="flex h-12 overflow-hidden rounded-lg border border-glass-border bg-input-bg transition-colors focus-within:border-primary">
                {mode === "register" && <span className="flex w-16 flex-none items-center justify-center border-r border-glass-border font-mono text-sm text-text-secondary">
                  +86
                </span>}
                <div className="relative min-w-0 flex-1">
                  {mode === "login" ? <UserRound
                    size={16}
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted"
                  /> : <Smartphone
                    size={16}
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted"
                  />}
                  <input
                    id={phoneId}
                    name={mode === "login" ? "identifier" : "phone"}
                    type={mode === "login" ? "text" : "tel"}
                    inputMode={mode === "login" ? "text" : "numeric"}
                    autoComplete={mode === "login" ? "username" : "tel-national"}
                    value={mode === "login" ? identifier : normalizedPhone}
                    onChange={(event) => setIdentifier(mode === "login" ? event.target.value : event.target.value.replace(/\D/g, "").slice(0, 11))}
                    placeholder={mode === "login" ? "请输入手机号或用户名" : "请输入手机号"}
                    className="h-full w-full bg-transparent pl-10 pr-3 text-sm text-foreground outline-none placeholder:text-text-muted"
                    disabled={submitting}
                  />
                </div>
              </div>
            </div>

            <div>
              <label htmlFor={passwordId} className="mb-2 block text-sm font-medium text-foreground">
                密码
              </label>
              <div className="relative h-12 rounded-lg border border-glass-border bg-input-bg transition-colors focus-within:border-primary">
                <LockKeyhole
                  size={16}
                  className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted"
                />
                <input
                  id={passwordId}
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete={mode === "login" ? "current-password" : "new-password"}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={mode === "login" ? "请输入密码" : "至少 10 位，包含字母和数字"}
                  className="h-full w-full bg-transparent pl-10 pr-12 text-sm text-foreground outline-none placeholder:text-text-muted"
                  disabled={submitting}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((visible) => !visible)}
                  className="absolute right-1.5 top-1/2 grid h-9 w-9 -translate-y-1/2 place-items-center rounded-md text-text-muted hover:bg-hover-bg hover:text-foreground"
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                  title={showPassword ? "隐藏密码" : "显示密码"}
                >
                  {showPassword ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              </div>
            </div>

            {mode === "register" && (
              <div>
                <label htmlFor={confirmationId} className="mb-2 block text-sm font-medium text-foreground">
                  确认密码
                </label>
                <input
                  id={confirmationId}
                  name="password-confirmation"
                  type={showPassword ? "text" : "password"}
                  autoComplete="new-password"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  placeholder="再次输入密码"
                  className="h-12 w-full rounded-lg border border-glass-border bg-input-bg px-3 text-sm text-foreground outline-none transition-colors placeholder:text-text-muted focus:border-primary"
                  disabled={submitting}
                />
              </div>
            )}

            {mode === "register" && inviteOnly && (
              <div>
                <label htmlFor={invitationId} className="mb-2 block text-sm font-medium text-foreground">
                  邀请码
                </label>
                <div className="relative h-12 rounded-lg border border-glass-border bg-input-bg transition-colors focus-within:border-primary">
                  <KeyRound size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-muted" />
                  <input
                    id={invitationId}
                    name="invitation-code"
                    type="text"
                    autoComplete="off"
                    value={invitationCode}
                    onChange={(event) => setInvitationCode(event.target.value)}
                    placeholder="请输入管理员提供的邀请码"
                    className="h-full w-full bg-transparent pl-10 pr-3 font-mono text-sm text-foreground outline-none placeholder:font-sans placeholder:text-text-muted"
                    disabled={submitting}
                  />
                </div>
              </div>
            )}

            {mode === "register" && (
              <p className="border-l-2 border-accent pl-3 text-xs leading-5 text-text-secondary">
                当前手机号尚未经过短信验证，仅作为登录账号使用。
              </p>
            )}

            {error && (
              <div
                role="alert"
                className="rounded-lg border border-status-failed-border bg-status-failed-bg px-3.5 py-3 text-sm text-status-failed-fg"
              >
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={submitting}
              className="flex h-12 w-full items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-bold text-on-accent shadow-[var(--glow-primary)] transition-colors hover:bg-primary-hover disabled:cursor-wait disabled:opacity-60"
            >
              {submitting && <Loader2 size={17} className="animate-spin" />}
              {submitting ? "正在处理" : mode === "login" ? "登录" : "创建账号"}
            </button>
          </form>
        </div>

        <p className="text-xs text-text-muted">手机号与创作数据均由平台安全保存</p>
      </section>

      <aside className="relative hidden min-h-screen overflow-hidden bg-surface-inset lg:block" aria-label="创作样片">
        <div className="grid h-screen grid-cols-[1.25fr_0.75fr] grid-rows-2 gap-2 p-2">
          <figure className="relative row-span-2 min-h-0 overflow-hidden rounded-md">
            <Image
              src={AUTH_IMAGES[0].src}
              alt={AUTH_IMAGES[0].alt}
              fill
              priority
              sizes="(min-width: 1024px) 44vw, 1px"
              className="object-cover"
            />
            <figcaption className="absolute bottom-4 left-4 rounded bg-background/80 px-2 py-1 font-mono text-xs text-foreground backdrop-blur-md">
              {AUTH_IMAGES[0].label} · 16:9
            </figcaption>
          </figure>
          {AUTH_IMAGES.slice(1).map((image) => (
            <figure key={image.src} className="relative min-h-0 overflow-hidden rounded-md">
              <Image
                src={image.src}
                alt={image.alt}
                fill
                sizes="(min-width: 1024px) 24vw, 1px"
                className="object-cover"
              />
              <figcaption className="absolute bottom-3 left-3 rounded bg-background/80 px-2 py-1 font-mono text-xs text-foreground backdrop-blur-md">
                {image.label}
              </figcaption>
            </figure>
          ))}
        </div>
        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between p-7">
          <div className="rounded-md border border-foreground/20 bg-black/55 px-3 py-2 font-mono text-xs text-white backdrop-blur-md">
            场次 {mode === "login" ? "续" : "初"} · 开机
          </div>
          <div className="h-2 w-20 rounded-full bg-primary shadow-[var(--glow-primary)]" />
        </div>
      </aside>
    </main>
  );
}
