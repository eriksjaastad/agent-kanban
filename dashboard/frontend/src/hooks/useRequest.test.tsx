import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useRequest } from './useRequest';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('useRequest', () => {
  it('returns data once the fetcher resolves', async () => {
    const { result } = renderHook(() => useRequest(async () => 'hello', []));
    expect(result.current.loading).toBe(true);
    await act(async () => {});
    expect(result.current).toMatchObject({ data: 'hello', error: null, loading: false });
  });

  it('reports a rejection as an error', async () => {
    const { result } = renderHook(() => useRequest(async () => { throw new Error('boom'); }, []));
    await act(async () => {});
    expect(result.current.error?.message).toBe('boom');
    expect(result.current.loading).toBe(false);
  });

  it('aborts the in-flight request on unmount', () => {
    const signals: AbortSignal[] = [];
    const { unmount } = renderHook(() => useRequest((signal) => {
      signals.push(signal);
      return new Promise<string>(() => {});
    }, []));
    expect(signals[0].aborted).toBe(false);
    unmount();
    expect(signals[0].aborted).toBe(true);
  });

  it('aborts the old run and ignores its late response when deps change', async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const signals: AbortSignal[] = [];
    const { result, rerender } = renderHook(
      ({ id }) => useRequest((signal) => {
        signals.push(signal);
        return id === 1 ? first.promise : second.promise;
      }, [id]),
      { initialProps: { id: 1 } },
    );
    rerender({ id: 2 });
    expect(signals[0].aborted).toBe(true);
    await act(async () => second.resolve('new'));
    await act(async () => first.resolve('old'));
    expect(result.current.data).toBe('new');
  });

  it('ignores a late failure from a superseded run', async () => {
    const first = deferred<string>();
    const { result, rerender } = renderHook(
      ({ id }) => useRequest(() => (id === 1 ? first.promise : Promise.resolve('new')), [id]),
      { initialProps: { id: 1 } },
    );
    rerender({ id: 2 });
    await act(async () => {});
    await act(async () => first.reject(new Error('stale')));
    expect(result.current).toMatchObject({ data: 'new', error: null });
  });

  it('turns a timeout into an error, even when the fetcher ignores its signal', async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const { result } = renderHook(() => useRequest((signal) => {
      signals.push(signal);
      return new Promise<string>(() => {});
    }, [], { timeoutMs: 1000 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.error?.message).toMatch(/timed out/);
    expect(result.current.loading).toBe(false);
    expect(signals[0].aborted).toBe(true);
  });

  it('reload runs the fetcher again, supersedes the previous run and keeps data on failure', async () => {
    const first = deferred<number>();
    let calls = 0;
    const { result } = renderHook(() => useRequest(async () => {
      calls += 1;
      if (calls === 1) return first.promise;
      if (calls === 2) return 2;
      throw new Error('later failure');
    }, []));
    await act(async () => result.current.reload());
    expect(result.current.data).toBe(2);
    await act(async () => first.resolve(1));
    expect(result.current.data).toBe(2);
    await act(async () => result.current.reload());
    expect(result.current.data).toBe(2);
    expect(result.current.error?.message).toBe('later failure');
  });

  it('reload after unmount starts nothing', () => {
    const fetcher = vi.fn(() => new Promise<string>(() => {}));
    const { result, unmount } = renderHook(() => useRequest(fetcher, []));
    expect(fetcher).toHaveBeenCalledTimes(1);
    unmount();
    result.current.reload();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
