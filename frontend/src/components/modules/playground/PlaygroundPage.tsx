'use client';

import { useEffect, useCallback, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { AlertCircle, Clock3, Coins, Sparkles } from 'lucide-react';
import ModeSelector from './ModeSelector';
import ModelSelector from './ModelSelector';
import MediaInput from './MediaInput';
import PromptInput from './PromptInput';
import ParameterBar from './ParameterBar';
import ResultGallery from './ResultGallery';
import { usePlaygroundStore, type PlaygroundMode, type PlaygroundGeneration, type QueuedRequest } from './usePlaygroundStore';
import {
  getSafeApiError,
  playgroundApi,
  userTicketApi,
  type PlaygroundGenerationResponse,
  type UserTicketWallet,
} from '@/lib/api';
import { IS_CLOUD_DEPLOYMENT } from '@/lib/deployment';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MODE_LABELS: Record<PlaygroundMode, string> = {
  t2i: 'T2I',
  i2i: 'I2I',
  t2v: 'T2V',
  i2v: 'I2V',
  r2v: 'R2V',
  v2v: 'V2V',
};

/** Modes that require media input (image or video source).
 *  t2i also shows optional media input — when provided, it auto-becomes i2i. */
const MODES_WITH_MEDIA: PlaygroundMode[] = ['i2i', 'i2v', 'r2v', 'v2v'];
const MODES_WITH_OPTIONAL_MEDIA: PlaygroundMode[] = ['t2i'];

/** Polling interval for generation status (ms) */
const POLL_INTERVAL = 2000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Convert API response to store-compatible PlaygroundGeneration */
function toGeneration(resp: PlaygroundGenerationResponse): PlaygroundGeneration {
  return {
    id: resp.id,
    mode: resp.mode as PlaygroundMode,
    model_id: resp.model_id,
    actual_model_name: resp.actual_model_name,
    actual_model_id: resp.actual_model_id,
    prompt: resp.prompt,
    negative_prompt: resp.negative_prompt,
    input_media: resp.input_media,
    parameters: resp.parameters,
    batch_size: resp.batch_size,
    outputs: resp.outputs.map((o) => ({
      id: o.id,
      media_reference: o.media_reference,
      media_id: o.media_id,
      media_url: o.media_url,
      media_type: o.media_type as 'image' | 'video',
      thumbnail_path: o.thumbnail_path,
      saved_to_library: o.saved_to_library,
    })),
    status: resp.status as PlaygroundGeneration['status'],
    raw_status: resp.raw_status,
    status_zh: resp.status_zh,
    cancellation_requested: resp.cancellation_requested,
    support_review: resp.support_review,
    support_review_reason: resp.support_review_reason,
    error: resp.error,
    created_at: resp.created_at,
    quoted_microtickets: resp.quoted_microtickets,
    quoted_tickets: resp.quoted_tickets,
    tokens_per_ticket: resp.tokens_per_ticket,
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function PlaygroundPage() {
  const t = useTranslations('playground');

  const mode = usePlaygroundStore((s) => s.mode);
  const modelId = usePlaygroundStore((s) => s.modelId);
  const prompt = usePlaygroundStore((s) => s.prompt);
  const negativePrompt = usePlaygroundStore((s) => s.negativePrompt);
  const inputMedia = usePlaygroundStore((s) => s.inputMedia);
  const parameters = usePlaygroundStore((s) => s.parameters);
  const batchSize = usePlaygroundStore((s) => s.batchSize);
  const history = usePlaygroundStore((s) => s.history);
  const setHistory = usePlaygroundStore((s) => s.setHistory);
  const setTemplates = usePlaygroundStore((s) => s.setTemplates);
  const startGeneration = usePlaygroundStore((s) => s.startGeneration);
  const updateGeneration = usePlaygroundStore((s) => s.updateGeneration);
  const enqueueRequest = usePlaygroundStore((s) => s.enqueueRequest);
  const markDispatching = usePlaygroundStore((s) => s.markDispatching);
  const removeFromQueue = usePlaygroundStore((s) => s.removeFromQueue);
  const queue = usePlaygroundStore((s) => s.queue);
  const activeCount = usePlaygroundStore((s) => s.activeGenerationIds.length);
  const maxConcurrent = usePlaygroundStore((s) => s.maxConcurrent);

  const pollTimers = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const [wallet, setWallet] = useState<UserTicketWallet | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const refreshWallet = useCallback(async () => {
    if (!IS_CLOUD_DEPLOYMENT) return;
    try {
      setWallet(await userTicketApi.getWallet());
    } catch {
      // A wallet refresh must not interrupt the active creative workflow.
    }
  }, []);

  // ─── Fetch initial data on mount ───────────────────────────────────────────

  useEffect(() => {
    void refreshWallet();
    playgroundApi.getHistory().then((items) => {
      setHistory(items.map(toGeneration));
    }).catch((err) => {
      console.error('[Playground] Failed to fetch history:', err);
    });

    playgroundApi.getTemplates().then((items) => {
      setTemplates(
        items.map((t) => ({
          id: t.id,
          name: t.name,
          category: t.category,
          prompt: t.prompt,
          negative_prompt: t.negative_prompt,
          default_mode: t.default_mode as PlaygroundMode | undefined,
          default_model_id: t.default_model_id,
          default_parameters: t.default_parameters,
          version: t.version,
          created_at: t.created_at,
          updated_at: t.updated_at,
        }))
      );
    }).catch((err) => {
      console.error('[Playground] Failed to fetch templates:', err);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshWallet]);

  // ─── Cleanup poll timers ───────────────────────────────────────────────────

  useEffect(() => {
    return () => {
      pollTimers.current.forEach((timer) => clearInterval(timer));
      pollTimers.current.clear();
    };
  }, []);

  // ─── Status poller ─────────────────────────────────────────────────────────

  const startPolling = useCallback((generationId: string) => {
    // Prevent duplicate timers
    if (pollTimers.current.has(generationId)) return;

    const timer = setInterval(async () => {
      try {
        const statusResp = await playgroundApi.getGenerationStatus(generationId);
        const isTerminal = statusResp.status === 'completed' || statusResp.status === 'failed';

        // Fetch full generation data for complete update
        const fullResp = await playgroundApi.getGeneration(generationId);
        updateGeneration(toGeneration(fullResp));

        if (isTerminal) {
          clearInterval(timer);
          pollTimers.current.delete(generationId);
          void refreshWallet();
        }
      } catch (err) {
        console.error('[Playground] Poll failed for', generationId, err);
        clearInterval(timer);
        pollTimers.current.delete(generationId);
      }
    }, POLL_INTERVAL);

    pollTimers.current.set(generationId, timer);
  }, [refreshWallet, updateGeneration]);

  // ─── Generate handler — enqueue a request; the dispatcher runs it ──────────

  const handleGenerate = useCallback(() => {
    if (!prompt.trim()) return;
    setSubmitError(null);
    // Auto-detect i2i: t2i + reference images -> i2i
    const effectiveMode = (mode === 't2i' && inputMedia.length > 0) ? 'i2i' : mode;
    enqueueRequest({
      mode: effectiveMode,
      modelId,
      prompt: prompt.trim(),
      negativePrompt: negativePrompt || undefined,
      inputMedia,
      parameters,
      batchSize,
    });
  }, [mode, modelId, prompt, negativePrompt, inputMedia, parameters, batchSize, enqueueRequest]);

  // ─── Queue dispatcher — POST a queued request, then poll for status ────────

  const dispatchRequest = useCallback(async (req: QueuedRequest) => {
    try {
      const resp = await playgroundApi.generate({
        mode: req.mode,
        model_id: IS_CLOUD_DEPLOYMENT ? undefined : req.modelId,
        prompt: req.prompt,
        negative_prompt: req.negativePrompt || undefined,
        input_media: req.inputMedia.length > 0 ? req.inputMedia : undefined,
        parameters: Object.keys(req.parameters).length > 0 ? req.parameters : undefined,
        batch_size: req.batchSize > 1 ? req.batchSize : undefined,
      });
      const gen = toGeneration(resp);
      startGeneration(gen);
      removeFromQueue(req.id);
      void refreshWallet();
      if (gen.status !== 'completed' && gen.status !== 'failed') {
        startPolling(gen.id);
      }
    } catch (err) {
      console.error('[Playground] Dispatch failed:', err);
      setSubmitError(getSafeApiError(err).message);
      removeFromQueue(req.id);
    }
  }, [refreshWallet, startGeneration, removeFromQueue, startPolling]);

  // Pump: dispatch pending requests up to the concurrency limit.
  const pump = useCallback(() => {
    const s = usePlaygroundStore.getState();
    const dispatching = s.queue.filter((q) => q.status === 'dispatching').length;
    let slots = s.maxConcurrent - s.activeGenerationIds.length - dispatching;
    if (slots <= 0) return;
    for (const req of s.queue) {
      if (slots <= 0) break;
      if (req.status !== 'pending') continue;
      slots -= 1;
      markDispatching(req.id);
      dispatchRequest(req);
    }
  }, [markDispatching, dispatchRequest]);

  // Run the pump whenever the queue, in-flight count, or concurrency changes.
  useEffect(() => {
    pump();
  }, [queue, activeCount, maxConcurrent, pump]);

  // ─── Derived values ────────────────────────────────────────────────────────

  const resultCount = history.reduce((n, g) => n + g.outputs.length, 0);
  const showMediaInput = MODES_WITH_MEDIA.includes(mode) || MODES_WITH_OPTIONAL_MEDIA.includes(mode);
  const canGenerate = prompt.trim().length > 0;

  // ─── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full flex-col overflow-hidden text-foreground">
      {/* ═══ PAGE HEADER ═══ */}
      <header className="flex shrink-0 items-center justify-between border-b border-border-subtle px-7 py-5">
        <div className="flex flex-col gap-1">
          <span className="font-mono text-[0.625rem] font-medium uppercase tracking-[0.2em] text-text-muted">
            自由创作台
            <span className="text-primary font-semibold"> · {t('header.eyebrowAccent')}</span>
          </span>
          <div className="flex items-baseline gap-[10px]">
            <h1 className="font-display text-[1.625rem] md:text-[2.125rem] font-semibold tracking-tight text-foreground atelier-display">
              {t('header.title')}
            </h1>
            <span className="font-mono text-[0.6875rem] uppercase tracking-[0.1em] text-text-muted">
              {t('header.resultsCount', { count: resultCount })}
            </span>
          </div>
          <p className="font-mono text-text-muted text-[0.6875rem] tracking-[0.06em]">
            {t('header.subtitle')}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {IS_CLOUD_DEPLOYMENT && wallet && (
            <div className="hidden items-center gap-3 rounded-md border border-glass-border bg-glass px-3 py-2 text-xs text-text-secondary sm:flex">
              <span className="inline-flex items-center gap-1.5">
                <Coins size={14} className="text-primary" />
                可用 <strong className="font-mono font-semibold text-foreground">{wallet.available_tickets}</strong>
              </span>
              <span className="h-4 w-px bg-border-subtle" />
              <span className="inline-flex items-center gap-1.5">
                <Clock3 size={14} className="text-amber-300" />
                预扣 <strong className="font-mono font-semibold text-foreground">{wallet.held_tickets}</strong>
              </span>
            </div>
          )}
          <span className="atelier-badge rounded border border-glass-border bg-glass px-2 py-1 text-[0.625rem] uppercase tracking-[0.18em] text-text-muted">
            {MODE_LABELS[mode]}
          </span>
        </div>
      </header>

      {/* ═══ SPLIT LAYOUT ═══ */}
      <div className="flex flex-1 overflow-hidden min-h-0">
        {/* ─── LEFT: INPUT PANEL ─── */}
        <aside className="flex w-[420px] shrink-0 flex-col gap-3 overflow-y-auto border-r border-glass-border px-4 py-4 scrollbar-thin">
          {/* Mode */}
          <section className="glass-panel atelier-card rounded-[20px] px-5 py-5">
            <div className="mb-3 font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
              {t('compose.modeLabel')}
            </div>
            <ModeSelector />
          </section>

          {/* Prompt — first, the primary input */}
          <section className="glass-panel atelier-card rounded-[20px] px-5 py-5">
            <div className="mb-3 font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
              {t('compose.promptLabel')}
            </div>
            <PromptInput />
          </section>

          {/* Media Input (conditional) */}
          {showMediaInput && (
            <section className="glass-panel atelier-card rounded-[20px] px-5 py-5">
              <div className="mb-3 font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
                {t(
                  mode === 'v2v'
                    ? 'compose.mediaSourceVideo'
                    : mode === 'r2v'
                      ? 'compose.mediaRefMaterial'
                      : mode === 'i2v'
                        ? 'compose.mediaFirstFrame'
                        : 'compose.mediaReference'
                )}
              </div>
              <MediaInput />
            </section>
          )}

          {/* Cloud routing is server-owned; only desktop exposes a model picker. */}
          <section className="glass-panel atelier-card rounded-[20px] px-5 py-5 relative z-30">
            {!IS_CLOUD_DEPLOYMENT && (
              <>
                <div className="mb-3 font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
                  {t('compose.modelLabel')}
                </div>
                <ModelSelector />
                <div className="my-4 h-px bg-border-subtle" />
              </>
            )}
            <div className="mb-3 font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
              {t('compose.parametersLabel')}
            </div>
            <ParameterBar />
          </section>

          {/* Spacer to push generate button to bottom */}
          <div className="flex-1" />

          {/* Generate CTA (sticky) */}
          <div className="sticky bottom-0 -mx-4 -mb-4 border-t border-glass-border bg-transparent backdrop-blur-md px-4 pb-4 pt-4">
            {submitError && (
              <div className="mb-3 flex items-start gap-2 rounded-md border border-red-400/25 bg-red-400/10 px-3 py-2 text-xs leading-5 text-red-200">
                <AlertCircle size={14} className="mt-0.5 shrink-0" />
                <span>{submitError}</span>
              </div>
            )}
            <button
              type="button"
              onClick={handleGenerate}
              disabled={!canGenerate}
              className={[
                'inline-flex w-full items-center justify-center gap-[7px] rounded-full px-6 py-[13px]',
                "font-['Space_Grotesk',sans-serif] text-sm font-semibold",
                'bg-primary text-on-accent shadow-[var(--glow-primary)] transition-all duration-150 disabled:opacity-40 disabled:shadow-none',
                canGenerate
                  ? 'hover:bg-primary-hover hover:-translate-y-px cursor-pointer'
                  : 'cursor-not-allowed',
              ].join(' ')}
            >
              <Sparkles size={16} aria-hidden="true" />
              <span>
                {batchSize > 1
                  ? t('compose.generateBatch', { count: batchSize })
                  : t('compose.generate')}
              </span>
            </button>
          </div>
        </aside>

        {/* ─── RIGHT: RESULT GALLERY ─── */}
        <main className="flex flex-1 flex-col overflow-hidden min-w-0">
          <ResultGallery />
        </main>
      </div>
    </div>
  );
}
