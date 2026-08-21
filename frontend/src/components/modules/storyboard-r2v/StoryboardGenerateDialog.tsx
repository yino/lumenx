"use client";
/**
 * StoryboardGenerateDialog — wraps the LLM-driven '从剧本生成分镜' flow.
 *
 * Per Q grill outcomes:
 *   · Pre-flight runs in the dialog itself (no silent disabled button —
 *     user can always open the dialog and see WHY it's blocked + quick
 *     jump back to the Script step).
 *   · Confirm path replaces existing shots wholesale (clear-and-regenerate
 *     semantics, mirrors how a fresh 提取实体 → 生成分镜 onboarding feels).
 *   · Long-running call surfaces as a project-aware toast (not blocking
 *     overlay) so users can switch projects and learn when the other one
 *     finishes via the global ToastContainer.
 */
import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Wand2, X, AlertTriangle, ArrowRight, Film, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import WorkflowActionButton from "@/components/shared/WorkflowActionButton";

interface StoryboardGenerateDialogProps {
    isOpen: boolean;
    onClose: () => void;
    /** Currently-loaded project — all gating reads from here. */
    project: {
        id: string;
        title?: string;
        originalText?: string;
        original_text?: string;
        characters?: any[];
        frames?: any[];
    } | null;
    existingShotCount: number;
    /** Called when the user confirms. Dialog closes immediately; parent
     *  runs the API call in the background with toast feedback. */
    onConfirm: (maxClipSeconds: 15 | 30) => void;
    /** Jump to the Script step (used by the empty-text quick fix link). */
    onJumpToScript?: () => void;
    initialMaxClipSeconds?: 15 | 30;
    allowThirtySeconds?: boolean;
}

export default function StoryboardGenerateDialog({
    isOpen,
    onClose,
    project,
    existingShotCount,
    onConfirm,
    onJumpToScript,
    initialMaxClipSeconds = 15,
    allowThirtySeconds = false,
}: StoryboardGenerateDialogProps) {
    const t = useTranslations("storyboardGen");
    const [maxClipSeconds, setMaxClipSeconds] = useState<15 | 30>(initialMaxClipSeconds);

    useEffect(() => {
        if (isOpen) {
            setMaxClipSeconds(initialMaxClipSeconds === 30 && allowThirtySeconds ? 30 : 15);
        }
    }, [isOpen, initialMaxClipSeconds, allowThirtySeconds]);

    const text = (project as any)?.original_text ?? project?.originalText ?? "";
    const charsCount = project?.characters?.length ?? 0;
    const checks = useMemo(() => {
        return [
            {
                key: "text" as const,
                pass: text.trim().length >= 40,
                label: t("checkText"),
                hint: t("checkTextHint"),
            },
            {
                key: "chars" as const,
                pass: charsCount > 0,
                label: t("checkChars"),
                hint: t("checkCharsHint"),
            },
        ];
    }, [text, charsCount, t]);

    const allPass = checks.every((c) => c.pass);

    const handleConfirm = () => {
        if (!allPass) return;
        onClose();
        onConfirm(maxClipSeconds);
    };

    return (
        <AnimatePresence>
            {isOpen && (
                <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="fixed inset-0 z-[120] bg-overlay backdrop-blur-sm grid place-items-center p-4"
                    onClick={onClose}
                >
                    <motion.div
                        initial={{ scale: 0.96, opacity: 0 }}
                        animate={{ scale: 1, opacity: 1 }}
                        exit={{ scale: 0.96, opacity: 0 }}
                        transition={{ duration: 0.18 }}
                        className="w-full max-w-md rounded-2xl border border-glass-border bg-elevated shadow-[0_24px_64px_-12px_rgba(0,0,0,0.7)]"
                        onClick={(e) => e.stopPropagation()}
                    >
                        {/* Header */}
                        <header className="flex items-center justify-between gap-3 px-5 py-3 border-b border-glass-border">
                            <div className="flex items-center gap-2">
                                <Sparkles size={15} className="text-primary" />
                                <h2 className="text-display font-medium text-foreground">{t("title")}</h2>
                            </div>
                            <button
                                onClick={onClose}
                                aria-label={t("close")}
                                className="p-1.5 rounded-lg hover:bg-hover-bg text-text-muted hover:text-foreground transition-colors"
                            >
                                <X size={15} />
                            </button>
                        </header>

                        {/* Body */}
                        <div className="px-5 py-4 space-y-4">
                            {/* Project context line */}
                            <p className="text-body-sm text-text-secondary">
                                <span className="text-text-muted">{t("forProject")}</span>{" "}
                                <span className="text-foreground font-medium">{project?.title || "—"}</span>
                            </p>

                            {/* Pre-flight checks */}
                            <section>
                                <h3 className="mb-2 font-mono text-[0.625rem] uppercase tracking-[0.18em] text-text-muted">
                                    {t("preflightTitle")}
                                </h3>
                                <ul className="space-y-2">
                                    {checks.map((c) => (
                                        <li
                                            key={c.key}
                                            className={`flex items-start gap-2 rounded-md border px-3 py-2 ${
                                                c.pass
                                                    ? "border-status-completed-border bg-status-completed-bg/5"
                                                    : "border-accent/40 bg-accent/10"
                                            }`}
                                        >
                                            <span
                                                className={`mt-0.5 inline-flex h-4 w-4 items-center justify-center rounded-full text-[0.625rem] font-bold ${
                                                    c.pass ? "bg-status-completed-bg/20 text-status-completed-fg" : "bg-accent/20 text-accent"
                                                }`}
                                            >
                                                {c.pass ? "✓" : "!"}
                                            </span>
                                            <div className="min-w-0 flex-1">
                                                <p className="text-[0.78125rem] text-foreground">{c.label}</p>
                                                {!c.pass && (
                                                    <p className="text-[0.6875rem] text-text-muted mt-0.5">{c.hint}</p>
                                                )}
                                            </div>
                                        </li>
                                    ))}
                                </ul>
                                {!allPass && onJumpToScript && (
                                    <button
                                        onClick={onJumpToScript}
                                        className="mt-2 inline-flex items-center gap-1 text-[0.6875rem] text-primary hover:text-primary-hover transition-colors"
                                    >
                                        {t("goFixInScript")}
                                        <ArrowRight size={11} />
                                    </button>
                                )}
                            </section>

                            {/* Generation clip planning. This is deliberately
                                separate from editorial beats: one clip can hold
                                several beats, but the provider receives one task
                                capped at the selected limit. */}
                            <section>
                                <h3 className="mb-2 font-mono text-[0.625rem] uppercase tracking-[0.18em] text-text-muted">
                                    {t("planningTitle")}
                                </h3>
                                <div className="grid grid-cols-2 gap-2">
                                    {([15, 30] as const).map((seconds) => {
                                        const disabled = seconds === 30 && !allowThirtySeconds;
                                        const selected = maxClipSeconds === seconds;
                                        return (
                                            <button
                                                key={seconds}
                                                type="button"
                                                disabled={disabled}
                                                onClick={() => setMaxClipSeconds(seconds)}
                                                className={`rounded-xl border px-3 py-3 text-left transition-colors ${
                                                    selected
                                                        ? "border-primary bg-primary/10 text-foreground"
                                                        : "border-glass-border bg-glass text-text-secondary hover:border-primary/35"
                                                } disabled:cursor-not-allowed disabled:opacity-45`}
                                            >
                                                <span className="block text-sm font-semibold">
                                                    {t(seconds === 15 ? "planning15Title" : "planning30Title")}
                                                </span>
                                                <span className="mt-1 block text-[0.6875rem] leading-relaxed text-text-muted">
                                                    {t(seconds === 15 ? "planning15Body" : "planning30Body")}
                                                </span>
                                                {disabled && (
                                                    <span className="mt-1.5 block text-[0.625rem] text-accent">
                                                        {t("planning30Unavailable")}
                                                    </span>
                                                )}
                                            </button>
                                        );
                                    })}
                                </div>
                                <p className="mt-2 text-[0.6875rem] leading-relaxed text-text-muted">
                                    {t("planningHint")}
                                </p>
                            </section>

                            {/* Destructive warning when shots already exist */}
                            {allPass && existingShotCount > 0 && (
                                <div className="flex items-start gap-2 rounded-md border border-accent/40 bg-accent/10 px-3 py-2">
                                    <AlertTriangle size={13} className="text-accent mt-0.5 shrink-0" />
                                    <p className="text-[0.75rem] text-accent">
                                        {t("willReplaceWarning", { count: existingShotCount })}
                                    </p>
                                </div>
                            )}

                            {/* Healthy CTA hint */}
                            {allPass && existingShotCount === 0 && (
                                <p className="text-[0.75rem] text-text-muted flex items-center gap-1.5">
                                    <Film size={12} />
                                    {t("freshHint")}
                                </p>
                            )}
                        </div>

                        {/* Footer */}
                        <footer className="flex items-center justify-end gap-2 px-5 py-3 border-t border-glass-border">
                            <WorkflowActionButton
                                variant="ghost"
                                size="sm"
                                onClick={onClose}
                            >
                                {t("cancel")}
                            </WorkflowActionButton>
                            <WorkflowActionButton
                                variant="primary"
                                size="sm"
                                disabled={!allPass}
                                leftIcon={<Wand2 />}
                                onClick={handleConfirm}
                            >
                                {existingShotCount > 0
                                    ? t("replaceAndGenerate")
                                    : t("generate")}
                            </WorkflowActionButton>
                        </footer>
                    </motion.div>
                </motion.div>
            )}
        </AnimatePresence>
    );
}
