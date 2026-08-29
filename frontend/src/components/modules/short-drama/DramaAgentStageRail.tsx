"use client";

import { BookOpenText, Boxes, Clapperboard, Check } from "lucide-react";
import { useTranslations } from "next-intl";
import type { Project } from "@/store/projectStore";

export type DramaAgentStageId = "script" | "cast" | "storyboard_r2v";

export interface DramaAgentRunSummary {
    status: string;
    current_stage: string;
    stage_status: string;
    validation_report?: {
        status?: string;
        findings?: Array<{ severity?: string }>;
    };
    approval?: { decision?: string };
}

const STAGES: Array<{
    id: DramaAgentStageId;
    icon: typeof BookOpenText;
    labelKey: "stageOutline" | "stageAssets" | "stageEpisodes";
}> = [
    { id: "script", icon: BookOpenText, labelKey: "stageOutline" },
    { id: "cast", icon: Boxes, labelKey: "stageAssets" },
    { id: "storyboard_r2v", icon: Clapperboard, labelKey: "stageEpisodes" },
];

function stageReady(stage: DramaAgentStageId, project: Project): boolean {
    if (stage === "script") return project.originalText.trim().length > 0;
    if (stage === "cast") {
        return (project.characters.length + project.scenes.length + project.props.length) > 0;
    }
    return project.frames.length > 0;
}

export default function DramaAgentStageRail({
    activeStage,
    onStageChange,
    project,
    agentRun,
}: {
    activeStage: DramaAgentStageId;
    onStageChange: (stage: DramaAgentStageId) => void;
    project: Project;
    agentRun?: DramaAgentRunSummary | null;
}) {
    const t = useTranslations("shortDrama");
    const styleName = project.art_direction?.style_config?.name || t("styleUnset");
    const aspectRatio = project.model_settings?.storyboard_aspect_ratio || project.aspectRatio || "16:9";
    const segmentLimit = project.storyboard_segment_max_seconds ?? 15;

    return (
        <header className="shrink-0 border-b border-glass-border bg-background/92 backdrop-blur-2xl">
            <div className="flex h-14 items-center justify-between gap-4 px-6">
                <nav aria-label={t("agentName")} className="flex h-full items-stretch">
                    {STAGES.map((stage, index) => {
                        const Icon = stage.icon;
                        const active = activeStage === stage.id;
                        const ready = stageReady(stage.id, project);
                        return (
                            <button
                                key={stage.id}
                                type="button"
                                onClick={() => onStageChange(stage.id)}
                                className={`group relative flex min-w-[150px] items-center justify-center gap-2 px-5 text-sm transition-colors ${
                                    active ? "text-foreground" : "text-text-muted hover:text-text-secondary"
                                }`}
                            >
                                <span className={`grid h-6 w-6 place-items-center rounded-full border font-mono text-[0.625rem] ${
                                    active
                                        ? "border-primary/55 bg-primary/15 text-primary"
                                        : "border-glass-border bg-glass text-text-muted"
                                }`}>
                                    {ready ? <Check size={11} strokeWidth={2.5} /> : index + 1}
                                </span>
                                <Icon size={14} />
                                <span className="font-medium">{index + 1}. {t(stage.labelKey)}</span>
                                {active && <span className="absolute inset-x-4 bottom-0 h-0.5 rounded-full bg-primary" />}
                            </button>
                        );
                    })}
                </nav>

                <div className="hidden min-w-0 items-center gap-2 xl:flex">
                    <span className="max-w-[180px] truncate rounded-full border border-glass-border bg-glass px-3 py-1 font-mono text-[0.625rem] text-text-secondary">
                        {styleName}
                    </span>
                    <span className="rounded-full border border-glass-border bg-glass px-3 py-1 font-mono text-[0.625rem] text-text-secondary">
                        {aspectRatio}
                    </span>
                    <span className="rounded-full border border-primary/25 bg-primary/10 px-3 py-1 font-mono text-[0.625rem] text-primary">
                        {t("clipLimit", { seconds: segmentLimit })}
                    </span>
                    {agentRun && (
                        <span
                            data-testid="short-drama-agent-status"
                            className={`max-w-[220px] truncate rounded-full border px-3 py-1 font-mono text-[0.625rem] ${
                                agentRun.status === "blocked"
                                    ? "border-accent/45 bg-accent/10 text-accent"
                                    : agentRun.status === "needs_approval"
                                        ? "border-primary/40 bg-primary/10 text-primary"
                                        : "border-status-completed-border bg-status-completed-bg/10 text-status-completed-fg"
                            }`}
                            title={`${agentRun.current_stage} · ${agentRun.stage_status}`}
                        >
                            Agent · {agentRun.current_stage}
                            {agentRun.approval?.decision === "pending" ? " · 待审批" : ""}
                        </span>
                    )}
                </div>
            </div>
        </header>
    );
}
