"use client";

import { useEffect, useState } from "react";
import {
  LayoutGrid,
  Layers,
  LogOut,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  Sun,
  WalletCards,
  Wand2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import clsx from "clsx";
import { IS_CLOUD_DEPLOYMENT } from "@/lib/deployment";
import LumenXBranding from "./LumenXBranding";
import { authApi, getSafeApiError } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { useSettingsStore } from "@/store/settingsStore";
import { toast } from "@/store/toastStore";
import WorkspaceSwitcher from "@/components/workspace/WorkspaceSwitcher";

export type GlobalTab = "workspace" | "library" | "playground" | "wallet" | "settings";

interface GlobalSidebarProps {
  activeTab: GlobalTab;
  onTabChange: (tab: GlobalTab) => void;
}

const ALL_GLOBAL_NAV_ITEMS: { id: GlobalTab; icon: typeof LayoutGrid; hash: string }[] = [
  { id: "workspace", icon: LayoutGrid, hash: "#/" },
  { id: "library", icon: Layers, hash: "#/library" },
  { id: "playground", icon: Wand2, hash: "#/playground" },
  { id: "wallet", icon: WalletCards, hash: "#/wallet" },
  { id: "settings", icon: Settings, hash: "#/settings" },
];

export const GLOBAL_NAV_ITEMS = ALL_GLOBAL_NAV_ITEMS.filter(
  (item) => item.id !== "wallet" || IS_CLOUD_DEPLOYMENT,
);

const APP_VERSION = "v0.2.0";
const SIDEBAR_STORAGE_KEY = "lumenx-global-sidebar-collapsed";

function NavButton({
  active,
  collapsed,
  label,
  icon: Icon,
  onClick,
}: {
  active: boolean;
  collapsed: boolean;
  label: string;
  icon: typeof LayoutGrid;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      aria-label={collapsed ? label : undefined}
      title={collapsed ? label : undefined}
      className={clsx(
        "group relative flex h-11 w-full items-center rounded-xl transition-all duration-200",
        collapsed ? "justify-center px-2" : "justify-start gap-3 px-3.5",
        active
          ? "bg-foreground text-on-accent font-semibold shadow-[0_10px_28px_-18px_rgba(127,127,127,0.75)]"
          : "text-text-secondary hover:bg-hover-bg hover:text-foreground font-medium",
      )}
    >
      <Icon
        size={18}
        strokeWidth={1.8}
        className={clsx(
          "flex-shrink-0 transition-colors",
          active ? "text-on-accent" : "text-text-muted group-hover:text-foreground",
        )}
      />
      {!collapsed && <span className="truncate text-[0.8125rem]">{label}</span>}
    </button>
  );
}

/**
 * Desktop global navigation. The three creator destinations live in a calm,
 * collapsible left rail; device-local collapse state and the day/night choice
 * are persisted without affecting project data.
 */
export default function GlobalSidebar({ activeTab, onTabChange }: GlobalSidebarProps) {
  const t = useTranslations("nav");
  const user = useAuthStore((state) => state.user);
  const setAnonymous = useAuthStore((state) => state.setAnonymous);
  const theme = useSettingsStore((state) => state.theme);
  const setTheme = useSettingsStore((state) => state.setTheme);
  const [loggingOut, setLoggingOut] = useState(false);
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1");
    } catch {
      setCollapsed(false);
    }
  }, []);

  const handleNav = (id: GlobalTab, hash: string) => {
    onTabChange(id);
    window.location.hash = hash;
  };

  const toggleCollapsed = () => {
    setCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(SIDEBAR_STORAGE_KEY, next ? "1" : "0");
      } catch {
        // Local persistence is optional; the control still works in-memory.
      }
      return next;
    });
  };

  const isLight = theme.endsWith("-light");
  const toggleDayNight = () => setTheme(isLight ? "atelier-dark" : "atelier-light");

  const logout = async () => {
    setLoggingOut(true);
    try {
      await authApi.logout();
      setAnonymous(null);
      window.location.hash = "#/";
    } catch (error) {
      toast.error("退出失败", { body: getSafeApiError(error).message });
    } finally {
      setLoggingOut(false);
    }
  };

  return (
    <aside
      className={clsx(
        "relative z-30 hidden h-full flex-shrink-0 flex-col border-r border-glass-border bg-background/92 backdrop-blur-2xl transition-[width] duration-300 ease-out md:flex",
        collapsed ? "w-[76px]" : "w-56",
      )}
    >
      <div className={clsx("flex h-[76px] items-center border-b border-glass-border", collapsed ? "justify-center px-2" : "justify-between px-4")}>
        <button
          type="button"
          onClick={() => handleNav("workspace", "#/")}
          aria-label={t("workspaceAria")}
          className="min-w-0 transition-opacity hover:opacity-80"
        >
          <LumenXBranding size="sm" showSlogan={false} markOnly={collapsed} />
        </button>
        {!collapsed && (
          <button
            type="button"
            onClick={toggleCollapsed}
            aria-label="收起侧边栏"
            title="收起侧边栏"
            className="grid h-9 w-9 place-items-center rounded-xl text-text-muted transition-colors hover:bg-hover-bg hover:text-foreground"
          >
            <PanelLeftClose size={17} />
          </button>
        )}
      </div>

      {collapsed && (
        <button
          type="button"
          onClick={toggleCollapsed}
          aria-label="展开侧边栏"
          title="展开侧边栏"
          className="mx-auto mt-3 grid h-10 w-10 place-items-center rounded-xl border border-glass-border text-text-muted transition-colors hover:bg-hover-bg hover:text-foreground"
        >
          <PanelLeftOpen size={17} />
        </button>
      )}

      {IS_CLOUD_DEPLOYMENT && !collapsed && <WorkspaceSwitcher />}

      <nav className={clsx("flex flex-1 flex-col gap-1 overflow-y-auto", collapsed ? "px-3 pt-3" : "px-3 pt-5")} aria-label={t("mainNavAria")}>
        {GLOBAL_NAV_ITEMS.filter((item) => item.id !== "settings").map((item) => (
          <NavButton
            key={item.id}
            active={activeTab === item.id}
            collapsed={collapsed}
            label={t(item.id)}
            icon={item.icon}
            onClick={() => handleNav(item.id, item.hash)}
          />
        ))}
      </nav>

      <div className="border-t border-glass-border p-3">
        <button
          type="button"
          onClick={toggleDayNight}
          aria-label={isLight ? "切换到黑夜模式" : "切换到白天模式"}
          title={isLight ? "切换到黑夜模式" : "切换到白天模式"}
          className={clsx(
            "mb-1 flex h-11 w-full items-center rounded-xl border border-glass-border bg-surface/55 text-text-secondary transition-colors hover:bg-hover-bg hover:text-foreground",
            collapsed ? "justify-center px-2" : "justify-start gap-3 px-3.5",
          )}
        >
          {isLight ? <Moon size={18} /> : <Sun size={18} />}
          {!collapsed && <span className="text-[0.75rem] font-medium">{isLight ? "黑夜模式" : "白天模式"}</span>}
        </button>

        {IS_CLOUD_DEPLOYMENT && user && !collapsed && (
          <div className="my-2 flex items-center gap-2 border-y border-glass-border py-2">
            <button
              type="button"
              onClick={() => handleNav("settings", "#/settings")}
              className="min-w-0 flex-1 text-left"
              title="账号安全"
            >
              <span className="block truncate font-mono text-xs text-foreground">
                {user.username || user.phone?.replace(/(\+86\d{3})\d{4}(\d{4})/, "$1****$2") || user.account_label}
              </span>
            </button>
            <button
              type="button"
              onClick={() => void logout()}
              disabled={loggingOut}
              className="grid h-8 w-8 flex-none place-items-center rounded-lg text-text-muted hover:bg-hover-bg hover:text-foreground disabled:opacity-50"
              aria-label="退出登录"
              title="退出登录"
            >
              <LogOut size={15} />
            </button>
          </div>
        )}

        <NavButton
          active={activeTab === "settings"}
          collapsed={collapsed}
          label={t("settings")}
          icon={Settings}
          onClick={() => handleNav("settings", "#/settings")}
        />
        {!collapsed && <div className="px-3 pt-2 font-mono text-[0.625rem] tracking-wide text-text-muted">{APP_VERSION}</div>}
      </div>
    </aside>
  );
}
