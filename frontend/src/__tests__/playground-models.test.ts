import { describe, expect, it } from 'vitest';

import {
  getModelDuration,
  getModelsForMode,
  resolveModelForMode,
} from '@/components/modules/playground/playgroundModels';

describe('playground Wan 2.7 duration constraints', () => {
  it('matches the T2V provider range', () => {
    expect(getModelDuration('wan2.7-t2v')).toEqual({
      type: 'slider',
      min: 2,
      max: 15,
      step: 1,
      default: 5,
    });
  });

  it('matches the VideoEdit provider range', () => {
    expect(getModelDuration('wan2.7-videoedit')).toEqual({
      type: 'slider',
      min: 2,
      max: 10,
      step: 1,
      default: 5,
    });
  });
});

describe('playground model resolution', () => {
  it('keeps a model that supports the requested mode', () => {
    const t2vModel = getModelsForMode('t2v')[0].id;

    expect(resolveModelForMode('t2v', t2vModel)).toBe(t2vModel);
  });

  it('replaces an empty or incompatible model with a mode default', () => {
    const resolved = resolveModelForMode('t2v', 'wan2.7-i2v');

    expect(resolved).not.toBe('');
    expect(getModelsForMode('t2v').some((model) => model.id === resolved)).toBe(true);
  });
});
