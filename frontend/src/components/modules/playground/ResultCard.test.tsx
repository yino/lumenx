import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ResultCard from './ResultCard';
import type { PlaygroundGeneration } from './usePlaygroundStore';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

describe('ResultCard video output', () => {
  const generation: PlaygroundGeneration = {
    id: 'generation-video-1',
    mode: 't2v',
    model_id: 'wan2.7-t2v',
    prompt: '雨夜街道',
    input_media: [],
    parameters: { duration: 2 },
    batch_size: 1,
    outputs: [{
      id: 'output-1',
      media_reference: '/files/output/video.mp4',
      media_type: 'video',
      saved_to_library: false,
    }],
    status: 'completed',
    created_at: '2026-08-22T00:00:00Z',
  };

  it('renders the generated video with native playback controls', () => {
    const { container } = render(<ResultCard generation={generation} />);

    expect(container.querySelector('video')).toHaveAttribute('controls');
    expect(container.querySelector('video')).not.toHaveAttribute('muted');
    expect(screen.getByRole('button', { name: '播放视频' })).toBeInTheDocument();
  });

  it('keeps video controls from opening the detail panel', () => {
    const onOpenDetail = vi.fn();
    const { container } = render(
      <ResultCard generation={generation} onOpenDetail={onOpenDetail} />,
    );
    const video = container.querySelector('video');
    const card = container.querySelector('.atelier-asset-card');

    expect(video).not.toBeNull();
    expect(card).not.toBeNull();
    fireEvent.click(video!);
    expect(onOpenDetail).not.toHaveBeenCalled();
    fireEvent.click(card!);
    expect(onOpenDetail).toHaveBeenCalledWith(generation, 'output-1');
  });

  it('starts playback from the visible play button', () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
    render(<ResultCard generation={generation} />);

    fireEvent.click(screen.getByRole('button', { name: '播放视频' }));

    expect(play).toHaveBeenCalledTimes(1);
    play.mockRestore();
  });
});
