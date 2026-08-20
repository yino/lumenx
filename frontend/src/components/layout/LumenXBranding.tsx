"use client";

import { useState, useEffect } from "react";
import { useSettingsStore, type ThemePreset } from "@/store/settingsStore";

interface LumenXBrandingProps {
  size?: "sm" | "md";
  showSlogan?: boolean;
  markOnly?: boolean;
}

// Logo 变体按主题映射（Tasty Sam 同形电路枫叶，透明底）。
// atelier-dark 复用蓝色 logo-dark.png，再以中性白适配极简黑色导航。
const LOGO_SRC: Record<ThemePreset, string> = {
  "atelier-dark": "/logo-dark.png",
  "bridge-dark": "/logo-dark.png",
  "brand-dark": "/logo-dark.png",
  "atelier-light": "/logo-light-teal.png",
  "brand-light": "/logo-light.png",
};
const ATELIER_DARK_FILTER = "grayscale(1) brightness(2.4)";

export default function LumenXBranding({ size = "md", showSlogan = true, markOnly = false }: LumenXBrandingProps) {
  const logoSize = size === "sm" ? "w-9 h-9" : "w-14 h-14";
  const titleSize = size === "sm" ? "text-lg" : "text-xl";

  const theme = useSettingsStore((s) => s.theme);
  // SSR 与客户端首次渲染统一用默认主题，避免 logo src/filter 的 hydration
  // mismatch；挂载后切到实际主题。
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const activeTheme: ThemePreset = mounted ? theme : "atelier-dark";
  const logoSrc = LOGO_SRC[activeTheme] ?? "/logo-dark.png";
  const logoFilter = activeTheme === "atelier-dark" ? ATELIER_DARK_FILTER : undefined;

  return (
    <div>
      <div className="flex gap-3 items-center">
        <div className="flex-shrink-0">
          <img
            src={logoSrc}
            alt="LumenX"
            className={`${logoSize} object-contain`}
            style={logoFilter ? { filter: logoFilter } : undefined}
          />
        </div>
        {!markOnly && <div className="flex flex-col justify-center">
          <div className="flex items-baseline gap-0">
            <span className={`font-mono ${titleSize} font-bold tracking-tight text-foreground`}>
              LUMEN
            </span>
            <span className={`font-mono ${titleSize} font-black tracking-tight text-primary`}>
              X
            </span>
          </div>
          {size !== "sm" && (
            <span className="font-mono text-[0.6875rem] text-text-muted tracking-[0.2em] uppercase -mt-0.5">
              Studio
            </span>
          )}
        </div>}
      </div>
      {showSlogan && !markOnly && (
        <p className="font-mono atelier-display text-[0.5rem] text-text-muted tracking-[0.15em] text-center mt-2.5 uppercase">
          Render Noise into Narrative
        </p>
      )}
    </div>
  );
}
