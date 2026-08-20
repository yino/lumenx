'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, RefreshCw } from 'lucide-react';
import { useTranslations } from 'next-intl';
import { playgroundApi } from '@/lib/api';
import ResultGallery from './ResultGallery';
import { toPlaygroundGeneration } from './playgroundGeneration';
import { usePlaygroundStore } from './usePlaygroundStore';
import type { Project, Series } from '@/store/projectStore';

const ACTIVE_REFRESH_INTERVAL = 2000;

/** Dedicated home for every free-creation task and output. The page refreshes
 * active server jobs independently, so leaving the composer never strands a
 * pending task in the UI. */
interface CreationHistoryPageProps {
  series: Series[];
  seriesEpisodes: Record<string, Project[]>;
  projects: Project[];
  onDeleteProject: (id: string) => void;
}

export default function CreationHistoryPage({
  series,
  seriesEpisodes,
  projects,
  onDeleteProject,
}: CreationHistoryPageProps) {
  const t = useTranslations('playground');
  const history = usePlaygroundStore((state) => state.history);
  const setHistory = usePlaygroundStore((state) => state.setHistory);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const items = await playgroundApi.getHistory(100);
      if (!mountedRef.current) return;
      setHistory(items.map(toPlaygroundGeneration));
      setLoadError(null);
    } catch (error) {
      if (!mountedRef.current) return;
      console.error('[CreationHistory] Failed to fetch history:', error);
      setLoadError(t('historyPage.loadFailed'));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [setHistory, t]);

  useEffect(() => {
    mountedRef.current = true;
    void refresh(true);
    const timer = window.setInterval(() => {
      const hasActiveTask = usePlaygroundStore.getState().history.some(
        (item) => item.status === 'pending' || item.status === 'processing',
      );
      if (hasActiveTask) void refresh();
    }, ACTIVE_REFRESH_INTERVAL);

    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
    };
  }, [refresh]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden text-foreground">
      {loadError && (
        <div className="mx-6 mt-4 flex shrink-0 items-center justify-between gap-3 rounded-xl border border-red-400/25 bg-red-400/10 px-4 py-3 text-sm text-red-200">
          <span className="inline-flex items-center gap-2">
            <AlertCircle size={16} />
            {loadError}
          </span>
          <button
            type="button"
            onClick={() => void refresh(true)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-current/30 px-3 py-1.5 text-xs font-medium hover:bg-red-400/10"
          >
            <RefreshCw size={13} />
            {t('historyPage.retry')}
          </button>
        </div>
      )}

      {loading && history.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="inline-flex items-center gap-2 text-sm text-text-muted">
            <RefreshCw size={16} className="animate-spin" />
            {t('historyPage.loading')}
          </div>
        </div>
      ) : (
        <ResultGallery
          series={series}
          seriesEpisodes={seriesEpisodes}
          projects={projects}
          onDeleteProject={onDeleteProject}
        />
      )}
    </div>
  );
}
