"use client";

import { useEffect, type ReactNode } from "react";

import LumenXBranding from "@/components/layout/LumenXBranding";
import { IS_CLOUD_DEPLOYMENT } from "@/lib/deployment";
import { useAuthStore } from "@/store/authStore";
import { useWorkspaceStore } from "@/store/workspaceStore";

export default function WorkspaceGate({ children }: { children: ReactNode }) {
  const user = useAuthStore((state) => state.user);
  const status = useWorkspaceStore((state) => state.status);
  const error = useWorkspaceStore((state) => state.error);
  const initialize = useWorkspaceStore((state) => state.initialize);
  const reset = useWorkspaceStore((state) => state.reset);

  useEffect(() => {
    if (!IS_CLOUD_DEPLOYMENT || !user) return;
    void initialize(user.id, user.default_workspace_id).catch(() => undefined);
    return reset;
  }, [initialize, reset, user]);

  if (!IS_CLOUD_DEPLOYMENT) return <>{children}</>;

  if (status === "loading" || status === "idle") {
    return (
      <main className="grid min-h-screen place-items-center bg-background text-foreground">
        <div className="flex flex-col items-center gap-4" role="status" aria-live="polite">
          <LumenXBranding size="md" showSlogan={false} markOnly />
          <span className="text-sm text-text-secondary">正在进入工作区</span>
        </div>
      </main>
    );
  }

  if (status === "error") {
    return (
      <main className="grid min-h-screen place-items-center bg-background px-6 text-foreground">
        <section className="w-full max-w-sm border-y border-glass-border py-6 text-center">
          <h1 className="text-lg font-semibold">无法进入工作区</h1>
          <p className="mt-2 text-sm text-text-secondary">{error || "工作区加载失败"}</p>
          <button
            type="button"
            onClick={() => user && void initialize(user.id, user.default_workspace_id).catch(() => undefined)}
            className="mt-5 h-10 rounded-lg bg-primary px-4 text-sm font-semibold text-on-accent hover:bg-primary-hover"
          >
            重新加载
          </button>
        </section>
      </main>
    );
  }

  return <>{children}</>;
}
