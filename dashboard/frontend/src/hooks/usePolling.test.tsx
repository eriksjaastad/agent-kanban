import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePolling } from './usePolling';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('usePolling', () => {
  it('fetches immediately and then on every interval', async () => {
    const fetcher = vi.fn(async () => 'ok');
    const { result } = renderHook(() => usePolling(fetcher, 1000, []));
    await act(async () => {});
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe('ok');
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(fetcher).toHaveBeenCalledTimes(4);
  });

  it('never overlaps: the next run is scheduled only after the current one settles', async () => {
    let release: () => void = () => {};
    const fetcher = vi.fn(() => new Promise<string>((resolve) => { release = () => resolve('done'); }));
    renderHook(() => usePolling(fetcher, 1000, []));
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => release());
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('keeps polling and keeps the last data after a failure', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce('good')
      .mockRejectedValueOnce(new Error('down'))
      .mockResolvedValue('back');
    const { result } = renderHook(() => usePolling(fetcher, 1000, []));
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current).toMatchObject({ data: 'good', error: expect.objectContaining({ message: 'down' }) });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current).toMatchObject({ data: 'back', error: null });
  });

  it('stops and aborts the in-flight request on unmount', async () => {
    const signals: AbortSignal[] = [];
    const fetcher = vi.fn(async (signal: AbortSignal) => { signals.push(signal); return 'ok'; });
    const { unmount } = renderHook(() => usePolling(fetcher, 1000, []));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(2);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('aborts a pending request on unmount', () => {
    const signals: AbortSignal[] = [];
    const { unmount } = renderHook(() => usePolling((signal) => {
      signals.push(signal);
      return new Promise<string>(() => {});
    }, 1000, []));
    unmount();
    expect(signals[0].aborted).toBe(true);
  });

  it('restarts when deps change and ignores the superseded response', async () => {
    let resolveFirst: (v: string) => void = () => {};
    const { result, rerender } = renderHook(
      ({ id }) => usePolling(() => (id === 1 ? new Promise<string>((r) => { resolveFirst = r; }) : Promise.resolve('two')), 1000, [id]),
      { initialProps: { id: 1 } },
    );
    rerender({ id: 2 });
    await act(async () => {});
    await act(async () => resolveFirst('one'));
    expect(result.current.data).toBe('two');
  });

  it('times out a hung request, reports an error, and polls again', async () => {
    const fetcher = vi.fn(() => new Promise<string>(() => {}));
    const { result } = renderHook(() => usePolling(fetcher, 1000, [], { timeoutMs: 500 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(result.current.error?.message).toMatch(/timed out/);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('does not clear a standing error when a tick starts', async () => {
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error('down'))
      .mockReturnValue(new Promise<string>(() => {}));
    const { result } = renderHook(() => usePolling(fetcher, 1000, []));
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current).toMatchObject({ loading: true, data: null, error: expect.objectContaining({ message: 'down' }) });
  });

  it('leaves data and error untouched when the fetcher resolves undefined, and keeps polling', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce('good')
      .mockRejectedValueOnce(new Error('down'))
      .mockResolvedValueOnce(undefined)
      .mockResolvedValue('back');
    const { result } = renderHook(() => usePolling(fetcher, 1000, []));
    await act(async () => {});
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current).toMatchObject({ data: 'good', error: expect.objectContaining({ message: 'down' }), loading: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current).toMatchObject({ data: 'back', error: null });
  });
});
