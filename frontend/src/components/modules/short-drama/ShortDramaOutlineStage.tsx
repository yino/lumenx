"use client";

import { useState } from "react";
import { BookOpenText, Palette } from "lucide-react";
import { useTranslations } from "next-intl";
import ScriptProcessor from "@/components/modules/ScriptProcessor";
import ArtDirection from "@/components/modules/ArtDirection";

export default function ShortDramaOutlineStage() {
    const t = useTranslations("shortDrama");
    const [panel, setPanel] = useState<"script" | "style">("script");

    return (
        <div className="flex h-full min-h-0 flex-col overflow-hidden">
            <div className="shrink-0 border-b border-glass-border bg-surface px-6 py-2">
                <div className="inline-flex rounded-xl border border-glass-border bg-glass p-1">
                    <button
                        type="button"
                        onClick={() => setPanel("script")}
                        className={`inline-flex items-center gap-2 rounded-lg px-4 py-1.5 text-xs font-medium transition-colors ${
                            panel === "script" ? "bg-foreground text-background" : "text-text-secondary hover:text-foreground"
                        }`}
                    >
                        <BookOpenText size={13} />
                        {t("outlineScript")}
                    </button>
                    <button
                        type="button"
                        onClick={() => setPanel("style")}
                        className={`inline-flex items-center gap-2 rounded-lg px-4 py-1.5 text-xs font-medium transition-colors ${
                            panel === "style" ? "bg-foreground text-background" : "text-text-secondary hover:text-foreground"
                        }`}
                    >
                        <Palette size={13} />
                        {t("outlineStyle")}
                    </button>
                </div>
            </div>
            <div className="min-h-0 flex-1 overflow-hidden">
                {panel === "script" ? <ScriptProcessor /> : <ArtDirection />}
            </div>
        </div>
    );
}
