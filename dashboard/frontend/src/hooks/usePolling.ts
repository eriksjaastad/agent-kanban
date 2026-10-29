import { useEffect, useState } from 'react';
import type { DependencyList } from 'react';
import { callWithTimeout, initialRequestState, toError, useLatest } from './useRequest';
import type { Fetcher, RequestOptions, RequestState } from './useRequest';

/**
 * Runs `fetcher` immediately, then `intervalMs` after each run settles, so
 * requests never overlap. Restarts when `deps` change; stops on unmount. A
 * failed run keeps the last good `data` and the loop keeps going. A tick never
 * clears a standing `error`; only a successful run does. A fetcher that resolves
 * `undefined` means "nothing to report": `data` and `error` stay as they were
 * and the next tick is still scheduled.
 */
export function usePolling<T>(fetcher: Fetcher<T | undefined>, intervalMs: number, deps: DependencyList, options: RequestOptions = {}) {
  const { timeoutMs } = options;
  const fetcherRef = useLatest(fetcher);
  const [state, setState] = useState<RequestState<T>>(initialRequestState);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function tick() {
      setState(prev => (prev.loading ? prev : { ...prev, loading: true }));
      try {
        const data = await callWithTimeout(fetcherRef.current, controller.signal, timeoutMs);
        if (!controller.signal.aborted) {
          setState(prev => (data === undefined ? { ...prev, loading: false } : { data, error: null, loading: false }));
        }
      } catch (reason) {
        if (!controller.signal.aborted) setState(prev => ({ ...prev, error: toError(reason), loading: false }));
      }
      if (!controller.signal.aborted) timer = setTimeout(tick, intervalMs);
    }

    void tick();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- deps are the caller's
  }, [fetcherRef, intervalMs, timeoutMs, ...deps]);

  return state;
}
