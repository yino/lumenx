"use client";

import GlobalSidebar, { type GlobalTab } from "./GlobalSidebar";
import OfflineBanner from "./OfflineBanner";
import BottomTabBar from "./BottomTabBar";
import { IS_CLOUD_DEPLOYMENT } from "@/lib/deployment";
import WorkspaceSwitcher from "@/components/workspace/WorkspaceSwitcher";

interface AppShellProps {
  activeTab: GlobalTab;
  onTabChange: (tab: GlobalTab) => void;
  children: React.ReactNode;
}

export default function AppShell({ activeTab, onTabChange, children }: AppShellProps) {
  return (
    <div className="flex h-full w-full flex-col">
      <OfflineBanner />
      {IS_CLOUD_DEPLOYMENT && (
        <div className="relative z-40 border-b border-glass-border bg-surface/80 px-3 py-2 backdrop-blur-xl md:hidden">
          <WorkspaceSwitcher compact />
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <GlobalSidebar activeTab={activeTab} onTabChange={onTabChange} />
        <div className="min-w-0 flex-1 overflow-y-auto">{children}</div>
      </div>
      <BottomTabBar activeTab={activeTab} onTabChange={onTabChange} />
    </div>
  );
}
