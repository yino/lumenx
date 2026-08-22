'use client';

import { useState, useMemo, useCallback } from 'react';
import { useTranslations } from 'next-intl';
import { GalleryHorizontal, Grid3x3, Layers3, Sparkles } from 'lucide-react';
import { usePlaygroundStore, type PlaygroundGeneration } from './usePlaygroundStore';
import { toPlaygroundGeneration } from './playgroundGeneration';
import { playgroundApi } from '@/lib/api';
import ResultCard from './ResultCard';
import GalleryView from './GalleryView';
import DetailPanel from './DetailPanel';
import QueuePanel from './QueuePanel';
import ProjectCard, { deriveCover } from '@/components/project/ProjectCard';
import type { Project, Series } from '@/store/projectStore';
import { coverGradient, GRAIN_URL } from '@/lib/atelierCover';

type FilterType = 'all' | 'project' | 'image' | 'video';

const VIDEO_MODES = new Set(['t2v', 'i2v', 'r2v', 'v2v']);

export interface HistoryDateGroup {
  key: string;
  kind: 'today' | 'yesterday' | 'thisWeek' | 'month' | 'unknown';
  year?: number;
  month?: number;
}

/** Calendar grouping uses the viewer's local timezone and treats Monday as
 * the first day of the week. Today/yesterday always win over week grouping. */
export function getHistoryDateGroup(
  dateStr: string,
  now = new Date(),
): HistoryDateGroup {
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) {
    return { key: 'unknown', kind: 'unknown' };
  }

  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const weekStart = new Date(today);
  const daysSinceMonday = (today.getDay() + 6) % 7;
  weekStart.setDate(today.getDate() - daysSinceMonday);
  const itemDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());

  if (itemDay.getTime() === today.getTime()) {
    return { key: 'today', kind: 'today' };
  }
  if (itemDay.getTime() === yesterday.getTime()) {
    return { key: 'yesterday', kind: 'yesterday' };
  }
  if (itemDay >= weekStart && itemDay < yesterday) {
    return { key: 'this-week', kind: 'thisWeek' };
  }
  const year = date.getFullYear();
  const month = date.getMonth() + 1;
  return { key: `month-${year}-${month}`, kind: 'month', year, month };
}

interface ResultGalleryProps {
  series?: Series[];
  seriesEpisodes?: Record<string, Project[]>;
  projects?: Project[];
  onDeleteProject?: (id: string) => void;
}

type UnifiedContentItem =
  | { kind: 'series'; series: Series; episodes: Project[]; timestamp: string; key: string }
  | { kind: 'project'; project: Project; timestamp: string; key: string }
  | { kind: 'output'; gen: PlaygroundGeneration; outputIndex: number; timestamp: string; key: string }
  | { kind: 'gen'; gen: PlaygroundGeneration; timestamp: string; key: string };

type UnifiedGridItem =
  | UnifiedContentItem
  | { kind: 'divider'; label: string; key: string };

function numberTimestampToIso(value?: number): string {
  if (!value) return new Date(0).toISOString();
  return new Date(value < 10_000_000_000 ? value * 1000 : value).toISOString();
}

export function getProjectHistoryTimestamp(project: Project): string {
  const updated = Date.parse(project.updatedAt || '');
  if (Number.isFinite(updated)) return new Date(updated).toISOString();
  const created = Date.parse(project.createdAt || '');
  if (Number.isFinite(created)) return new Date(created).toISOString();
  const rawCreated = (project as Project & { created_at?: number }).created_at;
  return numberTimestampToIso(rawCreated);
}

function getSeriesHistoryTimestamp(series: Series): string {
  return numberTimestampToIso(series.updated_at || series.created_at);
}

function SeriesHistoryCard({ series, episodes }: { series: Series; episodes: Project[] }) {
  const cover = episodes.map(deriveCover).find(Boolean);
  const frameCount = episodes.reduce((sum, episode) => sum + (episode.frames?.length || 0), 0);
  const updatedAt = new Date(getSeriesHistoryTimestamp(series));
  const dateLabel = updatedAt.getTime() > 0 ? updatedAt.toLocaleDateString('zh-CN') : '';

  return (
    <article
      role="button"
      tabIndex={0}
      onClick={() => { window.location.hash = `#/series/${series.id}`; }}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          if (event.key === ' ') event.preventDefault();
          window.location.hash = `#/series/${series.id}`;
        }
      }}
      className="group cursor-pointer overflow-hidden rounded-2xl border border-glass-border bg-glass transition-all hover:-translate-y-0.5 hover:border-foreground/30"
    >
      <div className="relative aspect-[16/10] overflow-hidden bg-surface-inset">
        {cover ? (
          <img src={cover} alt={series.title} className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-[1.04]" />
        ) : (
          <div className="absolute inset-0" style={{ background: coverGradient(series.id || series.title) }} aria-hidden="true">
            <div className="absolute inset-0 mix-blend-overlay" style={{ backgroundImage: GRAIN_URL, opacity: 0.07 }} />
          </div>
        )}
        <div className="absolute inset-0 bg-gradient-to-b from-transparent from-35% to-black/70" />
        <span className="absolute left-3 top-3 inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-black/45 px-2.5 py-1 font-mono text-[0.59375rem] uppercase tracking-wider text-white/85 backdrop-blur-md">
          <Layers3 size={11} />
          系列
        </span>
        <div className="absolute bottom-3 left-4 right-4">
          <h3 className="truncate font-display text-[1.375rem] font-semibold leading-tight tracking-tight text-white drop-shadow-lg">
            {series.title}
          </h3>
          <p className="mt-1 font-mono text-[0.5625rem] uppercase tracking-wider text-white/65">
            {episodes.length} 集 · {frameCount} 镜头
          </p>
        </div>
      </div>
      <div className="flex items-center justify-between px-4 py-3.5">
        <div>
          <p className="text-xs font-medium text-foreground">进入系列工作区</p>
          {dateLabel && <p className="mt-1 font-mono text-[0.5625rem] text-text-muted">更新于 {dateLabel}</p>}
        </div>
        <span className="rounded-full border border-glass-border px-2.5 py-1 text-[0.625rem] text-text-secondary">
          {episodes.length} 集
        </span>
      </div>
    </article>
  );
}

export default function ResultGallery({
  series = [],
  seriesEpisodes = {},
  projects = [],
  onDeleteProject,
}: ResultGalleryProps = {}) {
  const {
    history,
    startGeneration,
    updateGeneration,
    useResultAsReference: applyResultAsReference,
  } = usePlaygroundStore();
  const t = useTranslations('playground');
  const [activeFilter, setActiveFilter] = useState<FilterType>('all');
  const [viewMode, setViewMode] = useState<'grid' | 'gallery'>('grid');
  const [detailGen, setDetailGen] = useState<PlaygroundGeneration | null>(null);
  const [detailOutputId, setDetailOutputId] = useState<string | undefined>(undefined);

  const handleOpenDetail = useCallback((gen: PlaygroundGeneration, outputId?: string) => {
    setDetailGen(gen);
    setDetailOutputId(outputId);
  }, []);

  const handleRetry = useCallback(async (gen: PlaygroundGeneration) => {
    try {
      const resp = await playgroundApi.generate({
        mode: gen.mode,
        model_id: gen.model_id,
        prompt: gen.prompt,
        negative_prompt: gen.negative_prompt || undefined,
        input_media: gen.input_media.length > 0 ? gen.input_media : undefined,
        parameters: Object.keys(gen.parameters).length > 0 ? gen.parameters : undefined,
        batch_size: gen.batch_size > 1 ? gen.batch_size : undefined,
      });
      const newGen: PlaygroundGeneration = {
        id: resp.id,
        mode: resp.mode as PlaygroundGeneration['mode'],
        model_id: resp.model_id,
        prompt: resp.prompt,
        negative_prompt: resp.negative_prompt,
        input_media: resp.input_media,
        parameters: resp.parameters,
        batch_size: resp.batch_size,
        outputs: [],
        status: resp.status as PlaygroundGeneration['status'],
        error: resp.error,
        created_at: resp.created_at,
      };
      startGeneration(newGen);
      // Poll for status
      const poll = setInterval(async () => {
        try {
          const s = await playgroundApi.getGenerationStatus(newGen.id);
          if (s.status === 'completed' || s.status === 'failed') {
            clearInterval(poll);
            const full = await playgroundApi.getGeneration(newGen.id);
            updateGeneration({
              ...newGen,
              status: full.status as PlaygroundGeneration['status'],
              outputs: full.outputs.map((o) => ({
                id: o.id,
                media_reference: o.media_reference,
                media_id: o.media_id,
                media_url: o.media_url,
                media_type: o.media_type as 'image' | 'video',
                thumbnail_path: o.thumbnail_path,
                saved_to_library: o.saved_to_library,
              })),
              error: full.error,
            });
          }
        } catch { clearInterval(poll); }
      }, 2000);
    } catch (err) {
      console.error('[Playground] Retry failed:', err);
    }
  }, [startGeneration, updateGeneration]);

  const handleResume = useCallback(async (gen: PlaygroundGeneration) => {
    try {
      const resumed = toPlaygroundGeneration(await playgroundApi.resumeGeneration(gen.id));
      startGeneration(resumed);
      const poll = setInterval(async () => {
        try {
          const status = await playgroundApi.getGenerationStatus(gen.id);
          if (status.status === 'completed' || status.status === 'failed') {
            clearInterval(poll);
            updateGeneration(toPlaygroundGeneration(await playgroundApi.getGeneration(gen.id)));
          }
        } catch {
          clearInterval(poll);
        }
      }, 2000);
    } catch (err) {
      console.error('[Playground] Remote task recovery failed:', err);
    }
  }, [startGeneration, updateGeneration]);

  const handleDelete = useCallback(async (gen: PlaygroundGeneration) => {
    try {
      await playgroundApi.deleteGeneration(gen.id);
      usePlaygroundStore.getState().removeGeneration(gen.id);
    } catch (err) {
      console.error('[Playground] Delete failed:', err);
    }
  }, []);

  // Image result → "Generate video": set the image as i2v reference and switch mode.
  const handleGenerateVideo = useCallback(
    (mediaPath: string) => applyResultAsReference(mediaPath, 'image', 'i2v'),
    [applyResultAsReference],
  );

  const filtered = useMemo(() => {
    if (activeFilter === 'project') return [];
    if (activeFilter === 'all') return history;
    if (activeFilter === 'image') {
      return history.filter((g) => !VIDEO_MODES.has(g.mode));
    }
    return history.filter((g) => VIDEO_MODES.has(g.mode));
  }, [history, activeFilter]);

  // Sort descending by created_at
  const sorted = useMemo(
    () =>
      [...filtered].sort(
        (a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
      ),
    [filtered],
  );

  const groupingNow = useMemo(() => new Date(), [history]);

  const formatGroupLabel = useCallback((group: HistoryDateGroup): string => {
    if (group.kind === 'today') return t('results.today');
    if (group.kind === 'yesterday') return t('results.yesterday');
    if (group.kind === 'thisWeek') return t('results.thisWeek');
    if (group.kind === 'month' && group.month != null && group.year != null) {
      return group.year === groupingNow.getFullYear()
        ? t('results.month', { month: group.month })
        : t('results.yearMonth', { year: group.year, month: group.month });
    }
    return t('results.earlier');
  }, [groupingNow, t]);

  // Build natural calendar sections: today, yesterday, this week, then month.
  const itemsWithDividers = useMemo(() => {
    const result: Array<
      | { type: 'generation'; data: PlaygroundGeneration }
      | { type: 'divider'; label: string; key: string }
    > = [];

    let previousGroupKey: string | null = null;
    for (const generation of sorted) {
      const group = getHistoryDateGroup(generation.created_at, groupingNow);
      if (group.key !== previousGroupKey) {
        result.push({
          type: 'divider',
          label: formatGroupLabel(group),
          key: `divider-${group.key}`,
        });
        previousGroupKey = group.key;
      }
      result.push({ type: 'generation', data: generation });
    }

    return result;
  }, [formatGroupLabel, groupingNow, sorted]);

  const galleryGroupLabels = useMemo(() => {
    const labels: Record<string, string> = {};
    let pendingLabel: string | null = null;
    for (const item of itemsWithDividers) {
      if (item.type === 'divider') {
        pendingLabel = item.label;
      } else if (pendingLabel) {
        labels[item.data.id] = pendingLabel;
        pendingLabel = null;
      }
    }
    return labels;
  }, [itemsWithDividers]);

  // Flat list of generation data items (no dividers) for GalleryView and DetailPanel
  const dataItems = useMemo(
    () =>
      itemsWithDividers
        .filter((item): item is { type: 'generation'; data: PlaygroundGeneration } => item.type === 'generation')
        .map((item) => item.data),
    [itemsWithDividers],
  );

  // Project/series records and free-generation outputs share one chronological
  // stream. They keep their original storage and routes; this is a read model,
  // not a duplicate database.
  const unifiedGridItems = useMemo<UnifiedGridItem[]>(() => {
    const content: UnifiedContentItem[] = [];
    const includeProjects = activeFilter === 'all' || activeFilter === 'project';

    if (includeProjects) {
      series.forEach((item) => {
        content.push({
          kind: 'series',
          series: item,
          episodes: seriesEpisodes[item.id] || [],
          timestamp: getSeriesHistoryTimestamp(item),
          key: `series-${item.id}`,
        });
      });
      projects.forEach((project) => {
        content.push({
          kind: 'project',
          project,
          timestamp: getProjectHistoryTimestamp(project),
          key: `project-${project.id}`,
        });
      });
    }

    sorted.forEach((generation) => {
      if (generation.status === 'completed' && generation.outputs.length > 0) {
        generation.outputs.forEach((_, outputIndex) => {
          content.push({
            kind: 'output',
            gen: generation,
            outputIndex,
            timestamp: generation.created_at,
            key: `output-${generation.id}-${outputIndex}`,
          });
        });
      } else {
        content.push({
          kind: 'gen',
          gen: generation,
          timestamp: generation.created_at,
          key: `generation-${generation.id}`,
        });
      }
    });

    content.sort(
      (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime(),
    );

    const withDividers: UnifiedGridItem[] = [];
    let previousGroupKey: string | null = null;
    for (const item of content) {
      const group = getHistoryDateGroup(item.timestamp, groupingNow);
      if (group.key !== previousGroupKey) {
        withDividers.push({
          kind: 'divider',
          label: formatGroupLabel(group),
          key: `unified-divider-${group.key}`,
        });
        previousGroupKey = group.key;
      }
      withDividers.push(item);
    }
    return withDividers;
  }, [activeFilter, formatGroupLabel, groupingNow, projects, series, seriesEpisodes, sorted]);

  const unifiedContentCount = useMemo(
    () => unifiedGridItems.filter((item) => item.kind !== 'divider').length,
    [unifiedGridItems],
  );

  const filters: { key: FilterType; label: string }[] = [
    { key: 'all', label: t('results.filterAll') },
    { key: 'project', label: t('historyPage.filterProjects') },
    { key: 'image', label: t('results.filterImage') },
    { key: 'video', label: t('results.filterVideo') },
  ];
  const galleryAvailable = activeFilter === 'image' || activeFilter === 'video';
  const showGallery = galleryAvailable && viewMode === 'gallery';

  return (
    <div className="flex flex-col flex-1 overflow-hidden min-w-0">
      {/* Compact controls; page identity already lives in the global sidebar. */}
      <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border-subtle px-6 py-4">
        <span className="rounded-full border border-glass-border bg-glass px-2.5 py-1 font-mono text-[0.625rem] text-text-secondary">
          {unifiedContentCount}
        </span>
          <div className="flex items-center gap-[2px] bg-surface-inset rounded-full p-1 atelier-pill-tabs">
            {filters.map((f) => (
              <button
                key={f.key}
                onClick={() => setActiveFilter(f.key)}
                className={`rounded-full px-4 py-2 text-[0.8125rem] font-medium text-center transition-all cursor-pointer ${
                  activeFilter === f.key
                    ? 'bg-surface text-foreground atelier-pill-tab-active'
                    : 'text-text-muted hover:text-foreground hover:bg-hover-bg'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>

          {galleryAvailable && (
          <div className="flex items-center gap-[2px] bg-surface-inset rounded-full p-1 atelier-pill-tabs">
            <button
              onClick={() => setViewMode('grid')}
              className={`rounded-full p-2 transition-all cursor-pointer ${
                viewMode === 'grid'
                  ? 'bg-surface text-foreground atelier-pill-tab-active'
                  : 'text-text-muted hover:text-foreground hover:bg-hover-bg'
              }`}
              title={t('results.gridView')}
            >
              <Grid3x3 className="w-4 h-4" />
            </button>
            <button
              onClick={() => setViewMode('gallery')}
              className={`rounded-full p-2 transition-all cursor-pointer ${
                viewMode === 'gallery'
                  ? 'bg-surface text-foreground atelier-pill-tab-active'
                  : 'text-text-muted hover:text-foreground hover:bg-hover-bg'
              }`}
              title={t('results.galleryView')}
            >
              <GalleryHorizontal className="w-4 h-4" />
            </button>
          </div>
          )}

        <div className="ml-auto">
          <QueuePanel />
        </div>
      </div>

      {/* Content area */}
      {unifiedContentCount === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center overflow-hidden min-w-0">
          <Sparkles className="mb-4 h-12 w-12 text-text-muted opacity-40" />
          <p className="mb-1 font-display atelier-display text-base text-foreground">
            {t('results.emptyTitle')}
          </p>
          <p className="text-xs text-text-muted">{t('results.emptyBody')}</p>
          <button
            type="button"
            onClick={() => { window.location.hash = '#/playground'; }}
            className="mt-5 rounded-full border border-glass-border bg-glass px-4 py-2 text-xs font-medium text-text-secondary transition-colors hover:bg-hover-bg hover:text-foreground"
          >
            {t('historyPage.goCreate')}
          </button>
        </div>
      ) : showGallery ? (
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          <GalleryView
            generations={dataItems}
            groupLabels={galleryGroupLabels}
            onOpenDetail={handleOpenDetail}
            onRetry={handleRetry}
          />
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto p-6">
          <div className="grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-4 content-start">
            {unifiedGridItems.map((it) => {
              if (it.kind === 'divider') {
                return (
                  <div
                    key={it.key}
                    role="heading"
                    aria-level={2}
                    className="col-span-full flex items-center gap-4 py-4"
                  >
                    <div className="h-px flex-1 bg-gradient-to-r from-transparent to-glass-border" />
                    <span className="inline-flex min-w-[5.5rem] items-center justify-center gap-2 whitespace-nowrap rounded-full border border-primary/35 bg-primary/10 px-4 py-2 font-display text-sm font-semibold tracking-[0.08em] text-foreground shadow-sm">
                      <span className="h-1.5 w-1.5 rounded-full bg-primary shadow-[0_0_10px_currentColor]" aria-hidden="true" />
                      {it.label}
                    </span>
                    <div className="h-px flex-1 bg-gradient-to-l from-transparent to-glass-border" />
                  </div>
                );
              }
              if (it.kind === 'output') {
                return (
                  <ResultCard
                    key={`${it.gen.id}-${it.outputIndex}`}
                    generation={it.gen}
                    outputIndex={it.outputIndex}
                    onRetry={handleRetry}
                    onResume={handleResume}
                    onDelete={handleDelete}
                    onGenerateVideo={handleGenerateVideo}
                    onOpenDetail={handleOpenDetail}
                  />
                );
              }
              if (it.kind === 'series') {
                return <SeriesHistoryCard key={it.key} series={it.series} episodes={it.episodes} />;
              }
              if (it.kind === 'project') {
                return (
                  <ProjectCard
                    key={it.key}
                    project={it.project}
                    onDelete={onDeleteProject || (() => undefined)}
                  />
                );
              }
              return (
                <ResultCard
                  key={it.key}
                  generation={it.gen}
                  onRetry={handleRetry}
                  onResume={handleResume}
                  onDelete={handleDelete}
                  onGenerateVideo={handleGenerateVideo}
                  onOpenDetail={handleOpenDetail}
                />
              );
            })}
          </div>
        </div>
      )}

      {/* Detail Panel */}
      {detailGen && (
        <DetailPanel
          generation={detailGen}
          allGenerations={dataItems}
          focusOutputId={detailOutputId}
          onClose={() => { setDetailGen(null); setDetailOutputId(undefined); }}
          onNavigate={(g) => handleOpenDetail(g)}
          onRetry={handleRetry}
          onResume={handleResume}
          onGenerateVideo={handleGenerateVideo}
        />
      )}
    </div>
  );
}
