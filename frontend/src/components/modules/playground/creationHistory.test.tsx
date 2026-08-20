import { describe, expect, it } from 'vitest';

import { GLOBAL_NAV_ITEMS } from '@/components/layout/GlobalSidebar';
import { toPlaygroundGeneration } from './playgroundGeneration';
import { getHistoryDateGroup, getProjectHistoryTimestamp } from './ResultGallery';
import type { Project } from '@/store/projectStore';

describe('creation history navigation', () => {
  it('uses the creator-first navigation order', () => {
    const mainItems = GLOBAL_NAV_ITEMS.filter(
      (item) => item.id !== 'wallet' && item.id !== 'settings',
    );

    expect(mainItems.map((item) => item.id)).toEqual([
      'playground',
      'workspace',
      'canvas',
      'library',
      'history',
    ]);
    expect(mainItems.at(-1)?.hash).toBe('#/history');
  });
});

describe('creation history generation mapping', () => {
  it('keeps remote task recovery metadata with the history item', () => {
    const generation = toPlaygroundGeneration({
      id: 'generation-1',
      mode: 'i2v',
      model_id: 'seedance-2.0-i2v',
      prompt: '雨夜武侠对峙',
      input_media: ['media://first-frame'],
      parameters: { duration: 5 },
      batch_size: 1,
      outputs: [],
      status: 'processing',
      provider_name: 'volcengine-ark',
      provider_task_id: 'cgt-remote-1',
      provider_request_id: 'request-1',
      created_at: '2026-08-21T00:00:00Z',
    });

    expect(generation).toMatchObject({
      status: 'processing',
      provider_name: 'volcengine-ark',
      provider_task_id: 'cgt-remote-1',
      provider_request_id: 'request-1',
    });
  });
});

describe('creation history date grouping', () => {
  const localDate = (
    year: number,
    month: number,
    day: number,
    hour = 12,
  ) => new Date(year, month - 1, day, hour).toISOString();
  const now = new Date(2026, 7, 21, 18); // Friday, 21 August 2026.

  it('prioritizes today and yesterday over week grouping', () => {
    expect(getHistoryDateGroup(localDate(2026, 8, 21), now).kind).toBe('today');
    expect(getHistoryDateGroup(localDate(2026, 8, 20), now).kind).toBe('yesterday');
  });

  it('groups earlier Monday-to-Sunday records into this week', () => {
    expect(getHistoryDateGroup(localDate(2026, 8, 18), now)).toMatchObject({
      key: 'this-week',
      kind: 'thisWeek',
    });
  });

  it('groups older current-year records by month without the year', () => {
    expect(getHistoryDateGroup(localDate(2026, 7, 9), now)).toMatchObject({
      key: 'month-2026-7',
      kind: 'month',
      year: 2026,
      month: 7,
    });
  });

  it('keeps year and month for records from previous years', () => {
    expect(getHistoryDateGroup(localDate(2025, 11, 3), now)).toMatchObject({
      key: 'month-2025-11',
      kind: 'month',
      year: 2025,
      month: 11,
    });
  });

  it('uses the latest project timestamp in the unified timeline', () => {
    const project = {
      id: 'project-1',
      title: '零矿纪元',
      createdAt: '2026-08-19T08:00:00.000Z',
      updatedAt: '2026-08-20T09:30:00.000Z',
    } as Project;

    expect(getProjectHistoryTimestamp(project)).toBe('2026-08-20T09:30:00.000Z');
  });
});
