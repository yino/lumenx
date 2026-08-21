"use client";

import { useEffect, useMemo, useState } from "react";
import {
    ArrowLeft,
    CheckSquare2,
    Clapperboard,
    Clock3,
    Film,
    Layers3,
    ListChecks,
    Plus,
    Sparkles,
    Users,
    MapPin,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import StoryboardR2V from "@/components/modules/StoryboardR2V";
import VideoAssembly from "@/components/modules/VideoAssembly";
import { useProjectStore, type Project } from "@/store/projectStore";
import { toast } from "@/store/toastStore";

type WorkspaceView = "episodes" | "storyboard" | "assembly";

function runtimeSeconds(project: Project): number {
    return Math.round((project.frames || []).reduce((total, frame: any) => {
        const duration = Number(frame?.duration || 0);
        return total + (Number.isFinite(duration) ? duration : 0);
    }, 0));
}

function projectStatus(project: Project): "pending" | "planned" | "completed" {
    if (project.merged_video_url) return "completed";
    if ((project.frames || []).length > 0) return "planned";
    return "pending";
}

export default function ShortDramaEpisodeStage() {
    const t = useTranslations("shortDrama");
    const currentProject = useProjectStore((state) => state.currentProject);
    const updateProject = useProjectStore((state) => state.updateProject);
    const [episodes, setEpisodes] = useState<Project[]>(currentProject ? [currentProject] : []);
    const [view, setView] = useState<WorkspaceView>("episodes");
    const [multiSelect, setMultiSelect] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
    const [showCreate, setShowCreate] = useState(false);
    const [newTitle, setNewTitle] = useState("");
    const [creating, setCreating] = useState(false);

    const supportsThirtySeconds = /seedance[-_. ]?2[-_. ]?5/i.test(
        currentProject?.model_settings?.r2v_model ?? "",
    );

    useEffect(() => {
        if (!currentProject) return;
        let cancelled = false;
        if (!currentProject.series_id) {
            setEpisodes([currentProject]);
            return;
        }
        api.getSeriesEpisodes(currentProject.series_id)
            .then((items: Project[]) => {
                if (!cancelled) setEpisodes(items.length > 0 ? items : [currentProject]);
            })
            .catch(() => {
                if (!cancelled) setEpisodes([currentProject]);
            });
        return () => { cancelled = true; };
    }, [currentProject?.id, currentProject?.series_id, currentProject?.frames, currentProject?.merged_video_url]);

    const orderedEpisodes = useMemo(
        () => [...episodes].sort((a, b) => (a.episode_number ?? 9999) - (b.episode_number ?? 9999)),
        [episodes],
    );

    if (!currentProject) return null;

    const changeClipLimit = (seconds: 15 | 30) => {
        if (seconds === 30 && !supportsThirtySeconds) {
            toast.warning(t("thirtyModelOnly"));
            return;
        }
        updateProject(currentProject.id, { storyboard_segment_max_seconds: seconds });
    };

    const openEpisode = (episode: Project) => {
        if (episode.id !== currentProject.id) {
            const hash = currentProject.series_id
                ? `#/series/${currentProject.series_id}/episode/${episode.id}`
                : `#/project/${episode.id}`;
            window.location.hash = hash;
            return;
        }
        setView("storyboard");
    };

    const createEpisode = async () => {
        if (!currentProject.series_id) {
            toast.info(t("noSeriesAdd"));
            return;
        }
        if (!newTitle.trim()) return;
        setCreating(true);
        try {
            await api.createEpisodeForSeries(
                currentProject.series_id,
                newTitle.trim(),
                orderedEpisodes.length + 1,
                currentProject.workflow_mode || "r2v",
            );
            const refreshed = await api.getSeriesEpisodes(currentProject.series_id);
            setEpisodes(refreshed);
            setNewTitle("");
            setShowCreate(false);
        } catch (error: any) {
            toast.error(error?.response?.data?.detail || error?.message || "创建分集失败");
        } finally {
            setCreating(false);
        }
    };

    if (view !== "episodes") {
        return (
            <div className="flex h-full min-h-0 flex-col overflow-hidden">
                <div className="flex h-12 shrink-0 items-center justify-between border-b border-glass-border bg-surface px-5">
                    <button
                        type="button"
                        onClick={() => setView("episodes")}
                        className="inline-flex items-center gap-2 text-xs text-text-secondary transition-colors hover:text-foreground"
                    >
                        <ArrowLeft size={13} />
                        {t("backToEpisodes")}
                    </button>
                    <div className="inline-flex rounded-lg border border-glass-border bg-glass p-1">
                        <button
                            type="button"
                            onClick={() => setView("storyboard")}
                            className={`rounded-md px-3 py-1 text-xs ${view === "storyboard" ? "bg-foreground text-background" : "text-text-secondary"}`}
                        >
                            {t("storyboardEditor")}
                        </button>
                        <button
                            type="button"
                            onClick={() => setView("assembly")}
                            className={`rounded-md px-3 py-1 text-xs ${view === "assembly" ? "bg-foreground text-background" : "text-text-secondary"}`}
                        >
                            {t("assembly")}
                        </button>
                    </div>
                </div>
                <div className="min-h-0 flex-1 overflow-hidden">
                    {view === "storyboard" ? <StoryboardR2V /> : <VideoAssembly />}
                </div>
            </div>
        );
    }

    return (
        <div className="flex h-full min-h-0 flex-col overflow-hidden bg-surface">
            <header className="shrink-0 border-b border-glass-border px-8 py-6">
                <div className="flex items-start justify-between gap-6">
                    <div>
                        <div className="flex items-center gap-3">
                            <h1 className="font-display text-2xl font-semibold text-foreground">{t("episodesTitle")}</h1>
                            <span className="rounded-full border border-glass-border bg-glass px-2.5 py-1 font-mono text-[0.625rem] text-text-muted">
                                {t("episodeCount", { count: orderedEpisodes.length })}
                            </span>
                        </div>
                        <p className="mt-1 text-sm text-text-secondary">{t("episodesSubtitle")}</p>
                    </div>
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={() => setMultiSelect((value) => !value)}
                            className={`inline-flex h-9 items-center gap-2 rounded-lg border px-3 text-xs transition-colors ${
                                multiSelect ? "border-primary/50 bg-primary/10 text-primary" : "border-glass-border bg-glass text-text-secondary hover:text-foreground"
                            }`}
                        >
                            <CheckSquare2 size={13} />
                            {t("batch")}
                        </button>
                        <button
                            type="button"
                            onClick={() => currentProject.series_id ? setShowCreate(true) : toast.info(t("noSeriesAdd"))}
                            className="inline-flex h-9 items-center gap-2 rounded-lg bg-foreground px-4 text-xs font-semibold text-background"
                        >
                            <Plus size={13} />
                            {t("addEpisode")}
                        </button>
                    </div>
                </div>

                <div className="mt-5 flex flex-wrap items-center gap-3 rounded-xl border border-glass-border bg-glass px-4 py-3">
                    <div className="flex items-center gap-2 text-xs text-text-secondary">
                        <Layers3 size={13} className="text-primary" />
                        {t("clipLimitHint")}
                    </div>
                    <div className="ml-auto inline-flex rounded-lg border border-glass-border bg-background/40 p-1">
                        {([15, 30] as const).map((seconds) => {
                            const selected = (currentProject.storyboard_segment_max_seconds ?? 15) === seconds;
                            const disabled = seconds === 30 && !supportsThirtySeconds;
                            return (
                                <button
                                    key={seconds}
                                    type="button"
                                    disabled={disabled}
                                    onClick={() => changeClipLimit(seconds)}
                                    title={disabled ? t("thirtyModelOnly") : undefined}
                                    className={`rounded-md px-3 py-1.5 font-mono text-[0.6875rem] transition-colors ${
                                        selected ? "bg-primary text-on-accent" : "text-text-muted hover:text-foreground"
                                    } disabled:cursor-not-allowed disabled:opacity-35`}
                                >
                                    {t("clipLimit", { seconds })}
                                </button>
                            );
                        })}
                    </div>
                </div>
            </header>

            <div className="min-h-0 flex-1 overflow-y-auto px-8 py-6 custom-scrollbar">
                {orderedEpisodes.length === 0 ? (
                    <div className="grid h-64 place-items-center rounded-2xl border border-dashed border-glass-border text-sm text-text-muted">
                        {t("noEpisodes")}
                    </div>
                ) : (
                    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2 2xl:grid-cols-3">
                        {orderedEpisodes.map((episode) => {
                            const status = projectStatus(episode);
                            const selected = selectedIds.has(episode.id);
                            return (
                                <article
                                    key={episode.id}
                                    className={`group overflow-hidden rounded-2xl border bg-glass transition-colors ${
                                        selected ? "border-primary/55" : "border-glass-border hover:border-foreground/25"
                                    }`}
                                >
                                    <div className="relative h-32 overflow-hidden border-b border-glass-border bg-[radial-gradient(circle_at_20%_0%,rgba(100,108,255,0.24),transparent_48%),linear-gradient(135deg,rgba(255,255,255,0.09),rgba(255,255,255,0.01))] p-4">
                                        <div className="flex items-start justify-between">
                                            <span className="grid h-9 w-9 place-items-center rounded-xl border border-white/10 bg-black/25 text-white/80">
                                                <Clapperboard size={16} />
                                            </span>
                                            <span className={`rounded-full border px-2 py-1 font-mono text-[0.5625rem] ${
                                                status === "completed"
                                                    ? "border-status-completed-border bg-status-completed-bg/10 text-status-completed-fg"
                                                    : status === "planned"
                                                        ? "border-primary/35 bg-primary/10 text-primary"
                                                        : "border-glass-border bg-black/20 text-text-muted"
                                            }`}>
                                                {t(status)}
                                            </span>
                                        </div>
                                        <p className="absolute bottom-4 left-4 rounded bg-background/65 px-2 py-1 font-mono text-[0.625rem] uppercase tracking-[0.18em] text-foreground/70 backdrop-blur-sm">
                                            EP.{String(episode.episode_number ?? 1).padStart(2, "0")}
                                        </p>
                                    </div>
                                    <div className="p-4">
                                        <div className="flex items-start gap-3">
                                            {multiSelect && (
                                                <input
                                                    type="checkbox"
                                                    checked={selected}
                                                    onChange={() => setSelectedIds((previous) => {
                                                        const next = new Set(previous);
                                                        if (next.has(episode.id)) next.delete(episode.id); else next.add(episode.id);
                                                        return next;
                                                    })}
                                                    className="mt-1 accent-primary"
                                                    aria-label={`选择 ${episode.title}`}
                                                />
                                            )}
                                            <div className="min-w-0 flex-1">
                                                <h2 className="truncate text-base font-semibold text-foreground">{episode.title}</h2>
                                                <div className="mt-3 flex flex-wrap gap-2 font-mono text-[0.625rem] text-text-muted">
                                                    <span className="inline-flex items-center gap-1"><Users size={10} />{t("characters", { count: episode.characters?.length ?? 0 })}</span>
                                                    <span className="inline-flex items-center gap-1"><MapPin size={10} />{t("scenes", { count: episode.scenes?.length ?? 0 })}</span>
                                                    <span className="inline-flex items-center gap-1"><ListChecks size={10} />{t("clips", { count: episode.frames?.length ?? 0 })}</span>
                                                    <span className="inline-flex items-center gap-1"><Clock3 size={10} />{t("runtime", { seconds: runtimeSeconds(episode) })}</span>
                                                </div>
                                            </div>
                                        </div>
                                        <button
                                            type="button"
                                            onClick={() => openEpisode(episode)}
                                            className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-lg border border-glass-border bg-background/35 py-2 text-xs font-medium text-text-secondary transition-colors hover:border-primary/35 hover:text-primary"
                                        >
                                            <Film size={12} />
                                            {t("edit")}
                                        </button>
                                    </div>
                                </article>
                            );
                        })}
                    </div>
                )}
            </div>

            {showCreate && (
                <div className="fixed inset-0 z-[140] grid place-items-center bg-overlay p-4 backdrop-blur-sm" onClick={() => setShowCreate(false)}>
                    <div className="w-full max-w-sm rounded-2xl border border-glass-border bg-elevated p-5" onClick={(event) => event.stopPropagation()}>
                        <div className="flex items-center gap-2">
                            <Sparkles size={15} className="text-primary" />
                            <h2 className="font-display text-lg text-foreground">{t("newEpisodeTitle")}</h2>
                        </div>
                        <input
                            autoFocus
                            value={newTitle}
                            onChange={(event) => setNewTitle(event.target.value)}
                            onKeyDown={(event) => { if (event.key === "Enter") void createEpisode(); }}
                            placeholder={t("newEpisodePlaceholder")}
                            className="mt-4 w-full rounded-xl border border-glass-border bg-glass px-3 py-2.5 text-sm text-foreground outline-none focus:border-primary/55"
                        />
                        <div className="mt-4 flex justify-end gap-2">
                            <button type="button" onClick={() => setShowCreate(false)} className="rounded-lg px-3 py-2 text-xs text-text-secondary">{t("cancel")}</button>
                            <button type="button" disabled={!newTitle.trim() || creating} onClick={() => void createEpisode()} className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-on-accent disabled:opacity-45">{t("create")}</button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
