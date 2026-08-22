import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { playgroundApi } from '@/lib/api';
import MediaInput, { fileMatchesAccept } from './MediaInput';
import { usePlaygroundStore } from './usePlaygroundStore';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock('./AssetPickerModal', () => ({
  default: ({ isOpen, onSelect }: { isOpen: boolean; onSelect: (path: string) => void }) =>
    isOpen ? <button onClick={() => onSelect('library/new.png')}>select-test-asset</button> : null,
}));

describe('MediaInput', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    usePlaygroundStore.getState().resetForScope();
    usePlaygroundStore.setState({ mode: 'i2i', inputMedia: ['uploads/old.png'] });
  });

  it('replaces a single reference on the first file selection', async () => {
    vi.spyOn(playgroundApi, 'uploadMedia').mockResolvedValue({
      media_reference: 'uploads/new.png',
    } as Awaited<ReturnType<typeof playgroundApi.uploadMedia>>);
    const { container } = render(<MediaInput />);

    fireEvent.click(screen.getByRole('button', { name: 'media.replaceFile' }));
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['image'], 'new.png', { type: 'image/png' })] },
    });

    await waitFor(() => {
      expect(usePlaygroundStore.getState().inputMedia).toEqual(['uploads/new.png']);
    });
  });

  it('overwrites rather than appends when selecting a library asset in a single-input mode', () => {
    render(<MediaInput />);

    fireEvent.click(screen.getByRole('button', { name: 'media.pickFromLibrary' }));
    fireEvent.click(screen.getByRole('button', { name: 'select-test-asset' }));

    expect(usePlaygroundStore.getState().inputMedia).toEqual(['library/new.png']);
  });

  it('rejects files whose MIME type is incompatible with the mode', () => {
    expect(fileMatchesAccept({ name: 'frame.png', type: 'image/png' }, 'image/*')).toBe(true);
    expect(fileMatchesAccept({ name: 'clip.mp4', type: 'video/mp4' }, 'image/*')).toBe(false);
  });

  it('allows selecting multiple i2v images in one file chooser action', async () => {
    usePlaygroundStore.setState({ mode: 'i2v', inputMedia: [] });
    vi.spyOn(playgroundApi, 'uploadMedia').mockImplementation(async (file: File) => ({
      media_reference: file.name,
    } as Awaited<ReturnType<typeof playgroundApi.uploadMedia>>));

    const { container } = render(<MediaInput />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;

    expect(input.multiple).toBe(true);
    fireEvent.change(input, {
      target: {
        files: [
          new File(['one'], 'one.png', { type: 'image/png' }),
          new File(['two'], 'two.png', { type: 'image/png' }),
          new File(['three'], 'three.png', { type: 'image/png' }),
        ],
      },
    });

    await waitFor(() => {
      expect(usePlaygroundStore.getState().inputMedia).toEqual([
        'one.png',
        'two.png',
        'three.png',
      ]);
    });
  });

  it('keeps at most nine i2v images and reports ignored files', async () => {
    usePlaygroundStore.setState({ mode: 'i2v', inputMedia: [] });
    vi.spyOn(playgroundApi, 'uploadMedia').mockImplementation(async (file: File) => ({
      media_reference: file.name,
    } as Awaited<ReturnType<typeof playgroundApi.uploadMedia>>));

    const { container } = render(<MediaInput />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    const files = Array.from({ length: 10 }, (_, index) =>
      new File([String(index)], `frame-${index}.png`, { type: 'image/png' })
    );

    fireEvent.change(input, { target: { files } });

    await waitFor(() => {
      expect(usePlaygroundStore.getState().inputMedia).toHaveLength(9);
    });
    expect(screen.getByRole('status')).toHaveTextContent('media.maxFilesNotice');
  });
});
