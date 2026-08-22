import { beforeEach, describe, expect, it } from 'vitest';

import {
  filterInputMediaForMode,
  usePlaygroundStore,
  type PlaygroundTemplate,
} from '@/components/modules/playground/usePlaygroundStore';

describe('playground input compatibility', () => {
  beforeEach(() => {
    usePlaygroundStore.getState().resetForScope();
  });

  it('uses explicit extensions before source-mode inference', () => {
    expect(
      filterInputMediaForMode(
        ['still.png', 'clip.mp4', 'media:17'],
        'i2i',
        'v2v',
      ),
    ).toEqual(['clip.mp4']);

    expect(filterInputMediaForMode(['media:17'], 'v2v', 'v2v')).toEqual(['media:17']);
  });

  it('clears incompatible media and parameters when the mode changes', () => {
    usePlaygroundStore.setState({
      mode: 'i2i',
      inputMedia: ['still.png', 'clip.mp4'],
      parameters: { size: '1024x1024' },
    });

    usePlaygroundStore.getState().setMode('v2v');

    expect(usePlaygroundStore.getState()).toMatchObject({
      mode: 'v2v',
      inputMedia: ['clip.mp4'],
      parameters: {},
    });
  });

  it('keeps up to nine image references when switching to i2v', () => {
    usePlaygroundStore.setState({
      mode: 'r2v',
      inputMedia: ['first.png', 'second.png', 'third.png'],
    });

    usePlaygroundStore.getState().setMode('i2v');

    expect(usePlaygroundStore.getState()).toMatchObject({
      mode: 'i2v',
      inputMedia: ['first.png', 'second.png', 'third.png'],
    });
  });

  it('trims multi-reference media when switching to a single-reference mode', () => {
    usePlaygroundStore.setState({
      mode: 'r2v',
      inputMedia: ['first.png', 'second.png', 'third.png'],
    });

    usePlaygroundStore.getState().setMode('i2i');

    expect(usePlaygroundStore.getState()).toMatchObject({
      mode: 'i2i',
      inputMedia: ['first.png'],
    });
  });

  it('keeps current inputs and parameters when the active mode is clicked again', () => {
    usePlaygroundStore.setState({
      mode: 'i2i',
      inputMedia: ['still.png'],
      parameters: { size: '1024x1024' },
    });

    usePlaygroundStore.getState().setMode('i2i');

    expect(usePlaygroundStore.getState()).toMatchObject({
      inputMedia: ['still.png'],
      parameters: { size: '1024x1024' },
    });
  });

  it('selects a compatible default model without mounting the model picker', () => {
    usePlaygroundStore.setState({ mode: 't2i', modelId: '' });

    usePlaygroundStore.getState().setMode('t2v');

    expect(usePlaygroundStore.getState().modelId).not.toBe('');
    expect(usePlaygroundStore.getState().modelId).not.toContain('i2v');
  });

  it('repairs an empty model when the active mode is selected again', () => {
    usePlaygroundStore.setState({ mode: 't2v', modelId: '' });

    usePlaygroundStore.getState().setMode('t2v');

    expect(usePlaygroundStore.getState().modelId).not.toBe('');
  });

  it('clears stale parameters when applying a template without defaults', () => {
    usePlaygroundStore.setState({
      mode: 'i2i',
      inputMedia: ['still.png'],
      parameters: { size: '1024x1024' },
    });
    const template: PlaygroundTemplate = {
      id: 'template-1',
      name: '视频模板',
      category: 'test',
      prompt: '雨夜追逐',
      default_mode: 't2v',
      default_parameters: {},
      version: 1,
      created_at: '2026-08-22T00:00:00Z',
      updated_at: '2026-08-22T00:00:00Z',
    };

    usePlaygroundStore.getState().applyTemplate(template);

    expect(usePlaygroundStore.getState()).toMatchObject({
      mode: 't2v',
      inputMedia: [],
      parameters: {},
    });
  });

  it('resets stale parameters when a result becomes a reference', () => {
    usePlaygroundStore.setState({ parameters: { duration: 15 } });
    usePlaygroundStore.getState().useResultAsReference('result.png', 'image', 'i2v');

    expect(usePlaygroundStore.getState()).toMatchObject({
      mode: 'i2v',
      inputMedia: ['result.png'],
      parameters: {},
    });
  });
});

describe('playground session results', () => {
  const generation = (id: string, status: 'processing' | 'completed' = 'processing') => ({
    id,
    mode: 't2i' as const,
    model_id: 'test-model',
    prompt: `prompt-${id}`,
    input_media: [],
    parameters: {},
    batch_size: 1,
    outputs: [],
    status,
    created_at: '2026-08-22T00:00:00.000Z',
  });

  beforeEach(() => {
    usePlaygroundStore.getState().resetForScope();
  });

  it('tracks only locally started generations for the current session', () => {
    usePlaygroundStore.getState().startGeneration(generation('local-1'));

    expect(usePlaygroundStore.getState().sessionGenerationIds).toEqual(['local-1']);
    expect(usePlaygroundStore.getState().history.map((item) => item.id)).toEqual(['local-1']);
  });

  it('keeps a just-started local result when a history refresh races it', () => {
    usePlaygroundStore.getState().startGeneration(generation('local-1'));
    usePlaygroundStore.getState().setHistory([generation('remote-1', 'completed')]);

    expect(usePlaygroundStore.getState().history.map((item) => item.id)).toEqual([
      'local-1',
      'remote-1',
    ]);
  });

  it('stops the generating flag when a task completes', () => {
    usePlaygroundStore.getState().startGeneration(generation('local-1'));
    expect(usePlaygroundStore.getState().isGenerating).toBe(true);

    usePlaygroundStore.getState().updateGeneration(generation('local-1', 'completed'));

    expect(usePlaygroundStore.getState().isGenerating).toBe(false);
    expect(usePlaygroundStore.getState().activeGenerationIds).toEqual([]);
  });

  it('clears session-only result ids when the client scope resets', () => {
    usePlaygroundStore.getState().startGeneration(generation('local-1'));
    usePlaygroundStore.getState().resetForScope();

    expect(usePlaygroundStore.getState().sessionGenerationIds).toEqual([]);
    expect(usePlaygroundStore.getState().history).toEqual([]);
  });
});
