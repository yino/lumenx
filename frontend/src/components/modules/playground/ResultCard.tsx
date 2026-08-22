'use client';

import { useState, useCallback, useRef } from 'react';
import { Ban, Download, Video, Copy, Check, Replace, Crown, Bookmark, Play } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { getSafeApiError, playgroundApi } from '@/lib/api';
import { getAssetUrl } from '@/lib/utils';
import { usePlaygroundStore, type PlaygroundGeneration } from './usePlaygroundStore';
import { IS_CLOUD_DEPLOYMENT } from '@/lib/deployment';

interface ResultCardProps {
  generation: PlaygroundGeneration;
  outputIndex?: number;
  onGenerateVideo?: (imagePath: string) => void;
  onRetry?: (generation: PlaygroundGeneration) => void;
  onResume?: (generation: PlaygroundGeneration) => void;
  onOpenDetail?: (generation: PlaygroundGeneration, outputId?: string) => void;
  onDelete?: (generation: PlaygroundGeneration) => void;
}

const MODE_LABELS: Record<string, string> = {
  t2v: 'T2V',
  i2v: 'I2V',
  r2v: 'R2V',
  v2v: 'V2V',
  t2i: 'T2I',
  i2i: 'I2I',
};

function formatTime(dateStr: string): string {
  const date = new Date(dateStr);
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

function getElapsedProgress(createdAt: string): number {
  const elapsed = Date.now() - new Date(createdAt).getTime();
  // Estimate ~60s for generation, cap at 90%
  const progress = Math.min(elapsed / 60000, 0.9);
  return progress * 100;
}

function actualModelLabel(generation: PlaygroundGeneration): string {
  return generation.actual_model_name || generation.model_id || generation.mode;
}

function FailedCard({ generation, onRetry, onResume, onDelete }: { generation: PlaygroundGeneration; onRetry?: (g: PlaygroundGeneration) => void; onResume?: (g: PlaygroundGeneration) => void; onDelete?: (g: PlaygroundGeneration) => void }) {
  const { prompt, model_id, mode, created_at, error } = generation;
  const t = useTranslations('playground');
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const billedReview = generation.support_review || generation.raw_status === 'support_review';
  const cancelled = generation.raw_status === 'cancelled';

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!error) return;
    navigator.clipboard.writeText(error).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="rounded-[20px] border border-status-failed-border bg-glass overflow-hidden">
      <div
        className="relative overflow-hidden bg-elevated flex flex-col items-center justify-center cursor-pointer"
        style={{ aspectRatio: expanded ? undefined : '16/9', minHeight: expanded ? 120 : undefined }}
        onClick={() => setExpanded((v) => !v)}
      >
        <div className="absolute inset-0 bg-status-failed-bg" />
        <div className="relative text-center px-4 py-3 w-full">
          <p className="font-mono text-[0.625rem] text-status-failed-fg mb-2">
            {billedReview ? '计费待复核' : cancelled ? '任务已取消' : t('card.failed')}
          </p>
          {billedReview && (
            <p className="mb-2 text-[0.625rem] leading-5 text-amber-200">
              供应商已产生费用，但结果处理未完成。平台正在复核，请勿重复提交。
            </p>
          )}
          {error && (
            <p className={`text-[0.625rem] text-text-muted leading-relaxed break-all ${expanded ? '' : 'line-clamp-2'}`}>
              {error}
            </p>
          )}
        </div>

        {/* Action bar */}
        <div className="relative flex items-center gap-2 pb-2">
          {onResume && generation.provider_name === 'dashscope' && generation.provider_task_id && !billedReview && (
            <button
              onClick={(e) => { e.stopPropagation(); onResume(generation); }}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded text-[0.625rem] font-medium text-primary bg-primary/10 hover:bg-primary/20 transition-colors"
            >
              ↻ 恢复查询
            </button>
          )}
          {onRetry && !billedReview && (
            <button
              onClick={(e) => { e.stopPropagation(); onRetry(generation); }}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded text-[0.625rem] font-medium text-primary bg-primary/10 hover:bg-primary/20 transition-colors"
            >
              ↻ {t('card.retry')}
            </button>
          )}
          {onDelete && (
            <button
              onClick={(e) => { e.stopPropagation(); onDelete(generation); }}
              className="inline-flex items-center gap-1 px-2 py-1 rounded text-[0.625rem] font-medium text-status-failed-fg/60 hover:text-status-failed-fg hover:bg-status-failed-bg transition-colors"
            >
              × {t('card.delete')}
            </button>
          )}
          {error && (
            <button
              onClick={handleCopy}
              className="inline-flex items-center gap-1 px-2 py-1 rounded text-[0.625rem] font-medium text-text-muted hover:text-foreground hover:bg-hover-bg transition-colors"
            >
              {copied ? <Check className="w-3 h-3 text-primary" /> : <Copy className="w-3 h-3" />}
              {copied ? t('card.copied') : t('card.copyError')}
            </button>
          )}
          <span className="text-[0.5625rem] text-text-muted ml-auto">
            {expanded ? t('card.collapse') : t('card.expand')}
          </span>
        </div>
      </div>

      <div className="px-3 py-[10px]">
        <p className="text-[0.6875rem] text-text-secondary line-clamp-2 mb-1.5">{prompt}</p>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[0.5625rem] bg-glass text-text-muted rounded px-[6px] py-[2px]">
            实际模型：{actualModelLabel(generation)}
          </span>
          <span className="font-mono text-[0.5625rem] text-text-muted">
            {formatTime(created_at)}
          </span>
        </div>
      </div>
    </div>
  );
}

function CompletedCard({ generation, outputIndex, onGenerateVideo, onOpenDetail }: { generation: PlaygroundGeneration; outputIndex: number; onGenerateVideo?: (path: string) => void; onOpenDetail?: (generation: PlaygroundGeneration, outputId?: string) => void }) {
  const { prompt, model_id, mode, outputs, created_at } = generation;
  const t = useTranslations('playground');
  const output = outputs[outputIndex];
  const isVideo = output?.media_type === 'video' || ['t2v', 'i2v', 'r2v', 'v2v'].includes(mode);
  const [saving, setSaving] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);

  const saved = output?.saved_to_library ?? false;
  const mediaUrl = getAssetUrl(output?.media_url || output?.media_reference);
  const updateGeneration = usePlaygroundStore((s) => s.updateGeneration);
  const applyResultAsReference = usePlaygroundStore((s) => s.useResultAsReference);
  const featuredByGen = usePlaygroundStore((s) => s.featuredByGen);
  const toggleFeatured = usePlaygroundStore((s) => s.toggleFeatured);
  const featured = output ? featuredByGen[generation.id] === output.id : false;

  const handleDownload = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!mediaUrl) return;
    try {
      const resp = await fetch(mediaUrl);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = output?.media_id || output?.media_reference?.split('/').pop() || 'download';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch {
      window.open(mediaUrl, '_blank');
    }
  }, [mediaUrl, output]);

  const handleSaveToLibrary = useCallback(async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!output || saving) return;
    setSaving(true);
    try {
      const newSaved = !saved;
      if (newSaved) {
        await playgroundApi.saveToLibrary(generation.id, output.id);
      }
      const updatedOutputs = generation.outputs.map((o) =>
        o.id === output.id ? { ...o, saved_to_library: newSaved } : o
      );
      updateGeneration({ ...generation, outputs: updatedOutputs });
    } catch (err) {
      console.error('[Playground] Save to library failed:', err);
    } finally {
      setSaving(false);
    }
  }, [generation, output, saved, saving, updateGeneration]);

  const handleUseAsReference = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    if (!output?.media_reference) return;
    applyResultAsReference(output.media_reference, output.media_type);
  }, [applyResultAsReference, output]);

  return (
    <div
      className={`group rounded-[20px] border bg-glass atelier-asset-card overflow-hidden transition cursor-pointer ${saved ? 'border-primary/40 ring-1 ring-primary/30' : 'border-glass-border hover:border-foreground/30'}`}
      onClick={() => onOpenDetail?.(generation, output.id)}
    >
      {/* Media area */}
      <div className="relative overflow-hidden bg-elevated" style={{ aspectRatio: '16/9' }}>
        {mediaUrl ? (
          isVideo ? (
            <video
              ref={videoRef}
              src={mediaUrl}
              controls
              playsInline
              preload="metadata"
              onClick={(e) => e.stopPropagation()}
              onPlay={() => setIsPlaying(true)}
              onPause={() => setIsPlaying(false)}
              className="h-full w-full object-cover"
            />
          ) : (
            <img src={mediaUrl} alt={prompt} className="w-full h-full object-cover" />
          )
        ) : (
          <div className="w-full h-full bg-gradient-to-br from-elevated to-surface" />
        )}

        {isVideo && mediaUrl && !isPlaying && (
          <button
            type="button"
            aria-label="播放视频"
            title="播放视频"
            onClick={(e) => {
              e.stopPropagation();
              const video = videoRef.current;
              if (!video) return;
              void video.play()
                .then(() => setIsPlaying(true))
                .catch(() => setIsPlaying(false));
            }}
            className="absolute left-1/2 top-1/2 z-[3] flex h-12 w-12 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-white/30 bg-black/65 text-white shadow-lg backdrop-blur-sm transition hover:scale-105 hover:bg-black/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            <Play className="ml-0.5 h-5 w-5 fill-current" />
          </button>
        )}

        {/* Amber halation overlay — only when saved to library */}
        {saved && (
          <div className="atelier-proj-halation pointer-events-none absolute inset-0 z-[1]" />
        )}

        {/* Top-left badges: featured (best-of-batch) + video mode */}
        {(featured || isVideo) && (
          <div className="absolute top-2 left-2 z-[3] flex items-center gap-1.5">
            {featured && (
              <span
                className="inline-flex items-center gap-1 font-mono text-[0.5625rem] uppercase tracking-[0.08em] bg-status-starred-bg text-status-starred-fg border border-status-starred-border rounded px-[6px] py-[2px] backdrop-blur-sm"
                title={t('card.featured')}
              >
                <Crown className="w-2.5 h-2.5 fill-status-starred-solid" />
                {t('card.featured')}
              </span>
            )}
            {isVideo && (
              <span className="font-mono text-[0.5625rem] bg-black/60 text-foreground/80 backdrop-blur-sm rounded px-[6px] py-[2px] uppercase">
                {MODE_LABELS[mode] || mode}
              </span>
            )}
          </div>
        )}

        {/* Saved pill top-right */}
        {saved && (
          <span className="absolute top-2 right-2 z-[2] atelier-badge font-mono text-[0.5625rem] bg-primary/15 text-primary border border-primary/30 rounded px-[6px] py-[2px] uppercase">
            {t('card.saved')}
          </span>
        )}

        {/* Bottom gradient toolbar — appears on hover */}
        <div className={`absolute ${isVideo ? 'bottom-10' : 'bottom-0'} left-0 right-0 z-[2] h-12 bg-gradient-to-t from-black/70 to-transparent flex items-end justify-end gap-1.5 px-3 pb-2.5 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none`}>
          <button
            onClick={handleDownload}
            className="pointer-events-auto w-7 h-7 rounded-full bg-elevated backdrop-blur-sm flex items-center justify-center hover:bg-hover-bg transition"
            title={t('card.download')}
          >
            <Download className="w-3.5 h-3.5 text-foreground" />
          </button>
          <button
            onClick={handleUseAsReference}
            className="pointer-events-auto w-7 h-7 rounded-full bg-elevated backdrop-blur-sm flex items-center justify-center hover:bg-hover-bg transition"
            title={t('card.useAsReference')}
          >
            <Replace className="w-3.5 h-3.5 text-foreground" />
          </button>
          {output?.media_type === 'image' && onGenerateVideo && (
            <button
              onClick={(e) => { e.stopPropagation(); onGenerateVideo(output.media_reference); }}
              className="pointer-events-auto w-7 h-7 rounded-full bg-elevated backdrop-blur-sm flex items-center justify-center hover:bg-hover-bg transition"
              title={t('card.generateVideo')}
            >
              <Video className="w-3.5 h-3.5 text-foreground" />
            </button>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); if (output) toggleFeatured(generation.id, output.id); }}
            className={`pointer-events-auto w-7 h-7 rounded-full backdrop-blur-sm flex items-center justify-center transition ${featured ? 'bg-status-starred-bg' : 'bg-elevated hover:bg-hover-bg'}`}
            title={t('card.featured')}
          >
            <Crown className={`w-3.5 h-3.5 ${featured ? 'text-status-starred-solid fill-status-starred-solid' : 'text-foreground'}`} />
          </button>
          <button
            onClick={handleSaveToLibrary}
            className={`pointer-events-auto w-7 h-7 rounded-full backdrop-blur-sm flex items-center justify-center transition ${saved ? 'bg-primary/15' : 'bg-elevated hover:bg-hover-bg'}`}
            title={saved ? t('card.saved') : t('card.saveToLibrary')}
          >
            <Bookmark className={`w-3.5 h-3.5 ${saved ? 'text-primary fill-current' : 'text-foreground'}`} />
          </button>
        </div>
      </div>

      {/* Info area */}
      <div className="px-3 py-[10px]">
        <p className="text-[0.6875rem] text-text-secondary line-clamp-2 mb-1.5">{prompt}</p>
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-[0.5625rem] bg-glass text-text-muted rounded px-[6px] py-[2px]">
            实际模型：{actualModelLabel(generation)}
          </span>
          {/* Size or resolution tag */}
          {generation.parameters.size && (
            <span className="font-mono text-[0.5625rem] bg-glass text-text-muted rounded px-[6px] py-[2px]">
              {(generation.parameters.size as string).replace('*', '×').replace('x', '×')}
            </span>
          )}
          {generation.parameters.resolution && !generation.parameters.size && (
            <span className="font-mono text-[0.5625rem] bg-glass text-text-muted rounded px-[6px] py-[2px]">
              {generation.parameters.resolution as string}
            </span>
          )}
          {/* Mode badge */}
          <span className="font-mono text-[0.5625rem] bg-primary/10 text-primary/70 rounded px-[6px] py-[2px] uppercase">
            {MODE_LABELS[mode] || mode}
          </span>
          <span className="font-mono text-[0.5625rem] text-text-muted ml-auto">{formatTime(created_at)}</span>
          {saved && (
            <span className="flex items-center gap-0.5 text-[0.5625rem] text-primary">
              <Bookmark className="w-2.5 h-2.5 fill-current" />
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ResultCard({ generation, outputIndex = 0, onGenerateVideo, onRetry, onResume, onOpenDetail, onDelete }: ResultCardProps) {
  const { status, prompt, model_id, mode, created_at } = generation;
  const t = useTranslations('playground');
  const updateGeneration = usePlaygroundStore((s) => s.updateGeneration);
  const [canceling, setCanceling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const handleCancel = async (event: React.MouseEvent) => {
    event.stopPropagation();
    if (canceling || generation.cancellation_requested) return;
    setCanceling(true);
    setCancelError(null);
    try {
      const task = await playgroundApi.cancelGeneration(generation.id);
      updateGeneration({
        ...generation,
        status: task.status,
        raw_status: task.raw_status,
        status_zh: task.status_zh,
        cancellation_requested: task.cancellation_requested,
        support_review: task.support_review,
        error: task.error || task.message,
      });
    } catch (error) {
      setCancelError(getSafeApiError(error).message);
    } finally {
      setCanceling(false);
    }
  };

  // ─── PROCESSING STATE ───────────────────────────────────────────────────────
  if (status === 'pending' || status === 'processing') {
    return (
      <div className="rounded-[20px] border border-glass-border bg-glass atelier-asset-card overflow-hidden">
        {/* Media area */}
        <div className="relative overflow-hidden bg-elevated" style={{ aspectRatio: '16/9' }}>
          {/* Skeleton shimmer */}
          <div className="absolute inset-0 overflow-hidden">
            <div
              className="absolute inset-0 animate-shimmer"
              style={{
                background:
                  'linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.03) 50%, transparent 100%)',
                backgroundSize: '200% 100%',
              }}
            />
          </div>

          {/* Centered spinner + text */}
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
            <div className="w-6 h-6 border-2 border-glass-border border-t-primary rounded-full animate-spin" />
            <span className="font-mono text-[0.625rem] text-text-muted uppercase">
              {status === 'pending' ? t('card.queued') : t('card.processing')}
            </span>
            {generation.cancellation_requested && (
              <span className="text-[0.625rem] text-amber-200">正在等待供应商确认取消与计费状态</span>
            )}
          </div>

          {/* Progress bar */}
          <div className="absolute bottom-0 left-0 right-0 h-[3px] bg-glass">
            <div
              className="h-full bg-primary transition-all duration-1000 ease-out"
              style={{ width: `${getElapsedProgress(created_at)}%` }}
            />
          </div>
        </div>

        {/* Info area */}
        <div className="px-3 py-[10px]">
          <p className="text-[0.6875rem] text-text-secondary line-clamp-2 mb-1.5">{prompt}</p>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[0.5625rem] bg-glass text-text-muted rounded px-[6px] py-[2px]">
              实际模型：{actualModelLabel(generation)}
            </span>
            {generation.quoted_tickets && (
              <span className="font-mono text-[0.5625rem] rounded bg-amber-300/10 px-[6px] py-[2px] text-amber-200">
                预扣 {generation.quoted_tickets} 算力券
              </span>
            )}
            <span className="font-mono text-[0.5625rem] text-text-muted">
              {formatTime(created_at)}
            </span>
            {IS_CLOUD_DEPLOYMENT && (
              <button
                type="button"
                onClick={handleCancel}
                disabled={canceling || generation.cancellation_requested}
                className="ml-auto inline-flex h-6 items-center gap-1 rounded border border-red-300/25 bg-red-400/10 px-2 text-[0.625rem] text-red-200 transition-colors hover:bg-red-400/20 disabled:cursor-wait disabled:opacity-50"
              >
                <Ban size={11} />
                {canceling ? '取消中' : generation.cancellation_requested ? '取消确认中' : '取消任务'}
              </button>
            )}
          </div>
          {cancelError && <p className="mt-2 text-[0.625rem] text-red-200">{cancelError}</p>}
        </div>
      </div>
    );
  }

  // ─── FAILED STATE ───────────────────────────────────────────────────────────
  if (status === 'failed') {
    return <FailedCard generation={generation} onRetry={onRetry} onResume={onResume} onDelete={onDelete} />;
  }

  // ─── COMPLETED STATE ────────────────────────────────────────────────────────
  return <CompletedCard generation={generation} outputIndex={outputIndex} onGenerateVideo={onGenerateVideo} onOpenDetail={onOpenDetail} />;
}
