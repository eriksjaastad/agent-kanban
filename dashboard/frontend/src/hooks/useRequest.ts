import { useCallback, useEffect, useRef, useState } from 'react';
import type { DependencyList } from 'react';

export type Fetcher<T> = (signal: AbortSignal) => Promise<T>;

export interface RequestState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
}

export interface RequestOptions {
  /** Abort the request and report an error after this many milliseconds. */
  timeoutMs?: number;
}

export const initialRequestState = { data: null, error: null, loading: true } as const;

export function toError(reason: unknown): Error {
  return reason instanceof Error ? reason : new Error(String(reason));
}

/**
 * Runs `fetcher` with an AbortSignal that also fires after `timeoutMs`. A
 * timeout rejects even when the fetcher ignores its signal.
 */
export async function callWithTimeout<T>(fetcher: Fetcher<T>, signal: AbortSignal, timeoutMs?: number): Promise<T> {
  if (!timeoutMs) return fetcher(signal);
  const inner = new AbortController();
  const forward = () => inner.abort();
  signal.addEventListener('abort', forward);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error(`Request timed out after ${timeoutMs}ms`));
      inner.abort();
    }, timeoutMs);
  });
  try {
    return await Promise.race([fetcher(inner.signal), timeout]);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', forward);
  }
}

/** Keeps the newest fetcher without making it a dependency of the caller's effect. */
export function useLatest<T>(value: T) {
  const ref = useRef(value);
  useEffect(() => {
    ref.current = value;
  });
  return ref;
}

/**
 * Runs one async fetcher on mount and whenever `deps` change. Every run gets
 * its own AbortController; a run is aborted by unmount, by a newer run (deps
 * change or `reload()`), and its late response is ignored. `reload()` does
 * nothing before the hook's first run or after unmount; call it from event
 * handlers, not from an effect that may run first. The last good `data` is
 * kept when a later run fails, so callers can show it with `error`.
 */
export function useRequest<T>(fetcher: Fetcher<T>, deps: DependencyList, options: RequestOptions = {}) {
  const { timeoutMs } = options;
  const fetcherRef = useLatest(fetcher);
  const controllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(false);
  const [state, setState] = useState<RequestState<T>>(initialRequestState);

  const run = useCallback(() => {
    if (!mountedRef.current) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setState(prev => (prev.loading ? prev : { ...prev, loading: true }));
    callWithTimeout(fetcherRef.current, controller.signal, timeoutMs).then(
      data => {
        if (!controller.signal.aborted) setState({ data, error: null, loading: false });
      },
      reason => {
        if (!controller.signal.aborted) setState(prev => ({ ...prev, error: toError(reason), loading: false }));
      },
    );
  }, [fetcherRef, timeoutMs]);

  useEffect(() => {
    mountedRef.current = true;
    run();
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- deps are the caller's
  }, [run, ...deps]);

  return { ...state, reload: run };
}
