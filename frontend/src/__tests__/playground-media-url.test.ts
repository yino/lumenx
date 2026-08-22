import { afterEach, describe, expect, it, vi } from 'vitest';

import { API_URL } from '@/lib/api';
import { getAssetUrl } from '@/lib/utils';

describe('playground media URL resolution', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('routes backend files through the same-origin Next proxy in development', () => {
    vi.stubEnv('NODE_ENV', 'development');

    expect(getAssetUrl('/files/output/image.png?signature=abc')).toBe(
      '/api-proxy/files/image.png?signature=abc',
    );
    expect(getAssetUrl('http://localhost:17177/files/output/video.mp4?signature=abc#frame')).toBe(
      '/api-proxy/files/video.mp4?signature=abc#frame',
    );
    expect(getAssetUrl('output/playground/images/result.png')).toBe(
      '/api-proxy/files/playground/images/result.png',
    );
  });

  it('does not proxy unrelated remote media', () => {
    vi.stubEnv('NODE_ENV', 'development');
    expect(getAssetUrl('https://cdn.example.com/files/image.png')).toBe(
      'https://cdn.example.com/files/image.png',
    );
  });

  it('removes the filesystem-only output prefix in production URLs', () => {
    vi.stubEnv('NODE_ENV', 'production');

    expect(getAssetUrl('/files/output/playground/images/result.png')).toBe(
      `${API_URL}/files/playground/images/result.png`,
    );
    expect(getAssetUrl(`${API_URL}/files/output/playground/videos/result.mp4?token=abc#frame`)).toBe(
      `${API_URL}/files/playground/videos/result.mp4?token=abc#frame`,
    );
  });
});
