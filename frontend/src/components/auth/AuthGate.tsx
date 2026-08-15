"use client";

import { useEffect, type ReactNode } from "react";
import Image from "next/image";

import {
  SESSION_EXPIRED_EVENT,
  authApi,
  getSafeAuthError,
  type SafeAPIError,
} from "@/lib/api";
import { IS_CLOUD_DEPLOYMENT } from "@/lib/deployment";
import { useAuthStore } from "@/store/authStore";

import AuthScreen from "./AuthScreen";

interface AuthGateProps {
  children: ReactNode;
}

export default function AuthGate({ children }: AuthGateProps) {
  const status = useAuthStore((state) => state.status);
  const error = useAuthStore((state) => state.error);
  const beginSessionCheck = useAuthStore((state) => state.beginSessionCheck);
  const setAuthenticated = useAuthStore((state) => state.setAuthenticated);
  const setAnonymous = useAuthStore((state) => state.setAnonymous);

  useEffect(() => {
    if (!IS_CLOUD_DEPLOYMENT) {
      setAuthenticated({
        id: "desktop",
        phone: "",
        phone_verified: false,
        phone_verification_status: "本地模式",
        is_platform_admin: true,
        default_workspace_id: "desktop",
      });
      return;
    }
    beginSessionCheck();
    let active = true;
    const handleSessionExpired = (event: Event) => {
      const detail = (event as CustomEvent<SafeAPIError>).detail;
      setAnonymous(detail?.message || "登录状态已失效，请重新登录");
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    authApi
      .currentUser()
      .then(({ user }) => {
        if (active) setAuthenticated(user);
      })
      .catch((error) => {
        if (!active) return;
        const safeError = getSafeAuthError(error);
        setAnonymous(safeError.code === "ACCOUNT_SUSPENDED" ? safeError.message : null);
      });
    return () => {
      active = false;
      window.removeEventListener(SESSION_EXPIRED_EVENT, handleSessionExpired);
    };
  }, [beginSessionCheck, setAnonymous, setAuthenticated]);

  if (status === "checking") {
    return (
      <main className="grid min-h-screen place-items-center bg-background text-foreground">
        <div className="flex flex-col items-center gap-4" role="status" aria-live="polite">
          <Image
            src="/logo-dark.png"
            alt="LumenX"
            width={56}
            height={56}
            className="h-14 w-14 object-contain [filter:hue-rotate(-64deg)_saturate(1.35)_brightness(1.08)]"
          />
          <span className="text-sm text-text-secondary">正在恢复创作现场</span>
        </div>
      </main>
    );
  }

  if (status === "anonymous") {
    return (
      <AuthScreen
        initialError={error}
        onAuthenticated={setAuthenticated}
      />
    );
  }

  return <>{children}</>;
}
