'use client';

import { useEffect, useCallback, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
  AlertCircle,
  ChevronDown,
  Clapperboard,
  Clock3,
  Coins,
  ImageIcon,
  Layers3,
  Paperclip,
  SlidersHorizontal,
  Sparkles,
} from 'lucide-react';
import ModeSelector from './ModeSelector';
import ModelSelector from './ModelSelector';
import MediaInput from './MediaInput';
import PromptInput from './PromptInput';
import ParameterBar from './ParameterBar';
import QueuePanel from './QueuePanel';
import { usePlaygroundStore, type PlaygroundMode, type QueuedRequest } from './usePlaygroundStore';
import { toPlaygroundGeneration } from './playgroundGeneration';
import {
  getSafeApiError,
  playgroundApi,
  userTicketApi,
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

/** Polling interval for generation status (ms) */
const POLL_INTERVAL = 2000;

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
  const setMode = usePlaygroundStore((s) => s.setMode);
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
  const [showMediaPanel, setShowMediaPanel] = useState(false);
  const [showControls, setShowControls] = useState(false);

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
      setHistory(items.map(toPlaygroundGeneration));
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

  useEffect(() => {
    if (MODES_WITH_MEDIA.includes(mode)) {
      setShowMediaPanel(true);
    } else if (inputMedia.length === 0) {
      setShowMediaPanel(false);
    }
  }, [inputMedia.length, mode]);

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
        updateGeneration(toPlaygroundGeneration(fullResp));

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
      const gen = toPlaygroundGeneration(resp);
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

  const supportsMediaInput = MODES_WITH_MEDIA.includes(mode) || mode === 't2i';
  const mediaPanelVisible = supportsMediaInput && (showMediaPanel || inputMedia.length > 0);
  const canGenerate = prompt.trim().length > 0;

  // ─── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex h-full flex-col overflow-hidden text-foreground">
      {/* ═══ PROMPT-FIRST COMPOSER ═══ */}
      <div className="flex min-h-0 flex-1 justify-center overflow-y-auto scrollbar-thin">
        <main className="flex w-full max-w-[1040px] flex-col px-5 pb-10 pt-7 md:px-8 lg:pt-10">
          <section className="mx-auto mb-6 flex w-full max-w-[900px] flex-col items-center text-center">
            <div className="mb-2 inline-flex items-center gap-2 rounded-full border border-glass-border bg-glass px-3 py-1.5 font-mono text-[0.625rem] uppercase tracking-[0.16em] text-text-muted">
              <Sparkles size={12} className="text-accent" />
              {t('compose.eyebrow')}
            </div>
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground md:text-[2rem]">
              {t('compose.heroTitle')}
            </h2>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-text-muted">
              {t('compose.heroSubtitle')}
            </p>
            <div className="mt-5 max-w-full">
              <ModeSelector compact />
            </div>
          </section>

          <section
            data-testid="playground-composer"
            className="relative z-20 mx-auto w-full max-w-[900px] overflow-visible rounded-[28px] border border-glass-border bg-surface/88 shadow-[0_26px_80px_-48px_rgba(0,0,0,0.8)] backdrop-blur-2xl"
          >
            <div className="px-6 pb-3 pt-6 md:px-8 md:pt-7">
              <div className="mb-3 flex items-center justify-between">
                <span className="font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
                  {t('compose.promptLabel')}
                </span>
                <span className="atelier-badge rounded-full border border-glass-border bg-glass px-2.5 py-1 font-mono text-[0.625rem] tracking-[0.12em] text-text-muted">
                  {MODE_LABELS[mode]}
                </span>
              </div>
              <PromptInput />
            </div>

            {mediaPanelVisible && (
              <div className="border-t border-border-subtle bg-surface-inset/35 px-6 py-5 md:px-8">
                <div className="mb-3 flex items-center justify-between">
                  <span className="font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
                    {t(
                      mode === 'v2v'
                        ? 'compose.mediaSourceVideo'
                        : mode === 'r2v'
                          ? 'compose.mediaRefMaterial'
                          : mode === 'i2v'
                            ? 'compose.mediaFirstFrame'
                            : 'compose.mediaReference'
                    )}
                  </span>
                  {!MODES_WITH_MEDIA.includes(mode) && inputMedia.length === 0 && (
                    <button
                      type="button"
                      onClick={() => setShowMediaPanel(false)}
                      className="text-xs text-text-muted transition-colors hover:text-foreground"
                    >
                      {t('compose.collapse')}
                    </button>
                  )}
                </div>
                <MediaInput />
              </div>
            )}

            {showControls && (
              <div className="relative z-30 border-t border-border-subtle bg-surface-inset/45 px-6 py-5 md:px-8">
                <div className={IS_CLOUD_DEPLOYMENT ? '' : 'grid gap-5 md:grid-cols-[minmax(220px,0.72fr)_minmax(0,1.28fr)]'}>
                  {!IS_CLOUD_DEPLOYMENT && (
                    <div>
                      <div className="mb-3 font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
                        {t('compose.modelLabel')}
                      </div>
                      <ModelSelector />
                    </div>
                  )}
                  <div>
                    <div className="mb-3 font-mono text-[0.625rem] font-semibold uppercase tracking-[0.16em] text-text-secondary">
                      {t('compose.parametersLabel')}
                    </div>
                    <ParameterBar />
                  </div>
                </div>
              </div>
            )}

            {submitError && (
              <div className="mx-6 mt-3 flex items-start gap-2 rounded-xl border border-red-400/25 bg-red-400/10 px-3 py-2 text-xs leading-5 text-red-200 md:mx-8">
                <AlertCircle size={14} className="mt-0.5 shrink-0" />
                <span>{submitError}</span>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-2 border-t border-border-subtle px-4 py-3 md:px-5">
              {supportsMediaInput && (
                <button
                  type="button"
                  aria-expanded={mediaPanelVisible}
                  onClick={() => setShowMediaPanel((current) => !current)}
                  className={`inline-flex h-10 items-center gap-2 rounded-full border px-3.5 text-xs font-medium transition-colors ${
                    mediaPanelVisible
                      ? 'border-primary/35 bg-primary/10 text-foreground'
                      : 'border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground'
                  }`}
                >
                  <Paperclip size={14} />
                  {t('compose.addMaterial')}
                  {inputMedia.length > 0 && (
                    <span className="rounded-full bg-primary px-1.5 py-0.5 font-mono text-[0.5625rem] text-on-accent">
                      {inputMedia.length}
                    </span>
                  )}
                </button>
              )}
              <button
                type="button"
                aria-expanded={showControls}
                onClick={() => setShowControls((current) => !current)}
                className={`inline-flex h-10 items-center gap-2 rounded-full border px-3.5 text-xs font-medium transition-colors ${
                  showControls
                    ? 'border-primary/35 bg-primary/10 text-foreground'
                    : 'border-glass-border bg-glass text-text-secondary hover:bg-hover-bg hover:text-foreground'
                }`}
              >
                <SlidersHorizontal size={14} />
                {t('compose.modelAndParams')}
                <ChevronDown size={13} className={`transition-transform ${showControls ? 'rotate-180' : ''}`} />
              </button>

              <div className="ml-auto flex items-center gap-2">
                {IS_CLOUD_DEPLOYMENT && wallet && (
                  <div className="hidden items-center gap-2 rounded-full border border-glass-border bg-glass px-3 py-2 text-[0.6875rem] text-text-secondary lg:flex">
                    <span className="inline-flex items-center gap-1">
                      <Coins size={13} className="text-primary" />
                      <strong className="font-mono font-semibold text-foreground">{wallet.available_tickets}</strong>
                    </span>
                    <span className="h-3.5 w-px bg-border-subtle" />
                    <span className="inline-flex items-center gap-1">
                      <Clock3 size={13} className="text-amber-300" />
                      <strong className="font-mono font-semibold text-foreground">{wallet.held_tickets}</strong>
                    </span>
                  </div>
                )}
                <QueuePanel />
              </div>

              <button
                type="button"
                onClick={handleGenerate}
                disabled={!canGenerate}
                className={[
                  'inline-flex h-11 min-w-[132px] items-center justify-center gap-2 rounded-full px-6',
                  "font-['Space_Grotesk',sans-serif] text-sm font-semibold",
                  'bg-primary text-on-accent shadow-[var(--glow-primary)] transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-35 disabled:shadow-none',
                  canGenerate ? 'hover:-translate-y-px hover:bg-primary-hover' : '',
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
          </section>

          <section className="mx-auto mt-8 w-full max-w-[900px]">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="font-display text-sm font-semibold text-foreground">
                {t('compose.quickTitle')}
              </h3>
              <span className="font-mono text-[0.625rem] text-text-muted">WORKFLOWS</span>
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              {([
                { targetMode: 't2i' as const, icon: ImageIcon, title: t('compose.quickT2i'), body: t('compose.quickT2iDesc') },
                { targetMode: 'i2v' as const, icon: Clapperboard, title: t('compose.quickI2v'), body: t('compose.quickI2vDesc') },
                { targetMode: 'r2v' as const, icon: Layers3, title: t('compose.quickR2v'), body: t('compose.quickR2vDesc') },
              ]).map(({ targetMode, icon: Icon, title: quickTitle, body }) => (
                <button
                  key={targetMode}
                  type="button"
                  onClick={() => {
                    setMode(targetMode);
                    setShowMediaPanel(targetMode !== 't2i');
                  }}
                  className="group flex items-start gap-3 rounded-2xl border border-glass-border bg-glass px-4 py-4 text-left transition-all hover:-translate-y-0.5 hover:border-foreground/25 hover:bg-hover-bg"
                >
                  <span className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-glass-border bg-surface-inset text-text-secondary transition-colors group-hover:text-foreground">
                    <Icon size={17} />
                  </span>
                  <span className="min-w-0">
                    <span className="block text-sm font-semibold text-foreground">{quickTitle}</span>
                    <span className="mt-1 block text-xs leading-5 text-text-muted">{body}</span>
                  </span>
                </button>
              ))}
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
