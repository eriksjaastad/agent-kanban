import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DashboardPage } from './DashboardPage';

// Panels that fetch on their own — not under test here.
vi.mock('./KanbanBreakdown', () => ({ KanbanBreakdown: () => null }));
vi.mock('./ShadowPricingPanel', () => ({ ShadowPricingPanel: () => null }));

const SNAPSHOT = {
  repos: [],
  open_pull_requests: [],
  recent_commits: [],
  workflow_runs: [],
  branches: [],
  summary: {
    total_repos: 1, archived_repos: 0, open_prs: 0, draft_prs: 0, recent_commit_count: 0,
    repos_with_ci: 0, failing_ci: 0, fetch_errors: 0, repos_not_on_github: 0,
  },
  fetched_at: '2026-10-08T00:00:00Z',
};
const REFRESHING = { refreshing: true, retry_after_seconds: 2 };
const FRESH = { ...SNAPSHOT, refreshing: false, retry_after_seconds: 300 };

function respond(...payloads: unknown[]) {
  const fetcher = vi.fn();
  for (const payload of payloads) {
    fetcher.mockResolvedValueOnce({ ok: true, json: async () => payload });
  }
  fetcher.mockResolvedValue({ ok: true, json: async () => FRESH });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

let hidden = false;

describe('DashboardPage — /api/github polling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    hidden = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    // Drop the own-property override so jsdom's prototype getter is back.
    delete (document as { hidden?: boolean }).hidden;
  });

  it('backs off while the server refreshes instead of polling every 2s', async () => {
    const fetcher = respond(REFRESHING, REFRESHING, REFRESHING, REFRESHING, REFRESHING, REFRESHING);
    await act(async () => { render(<MemoryRouter><DashboardPage /></MemoryRouter>); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await advance(4999);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(fetcher).toHaveBeenCalledTimes(2);   // 5s
    await advance(10000);
    expect(fetcher).toHaveBeenCalledTimes(3);   // +10s
    await advance(20000);
    expect(fetcher).toHaveBeenCalledTimes(4);   // +20s
    await advance(30000);
    expect(fetcher).toHaveBeenCalledTimes(5);   // +30s
    await advance(29999);
    expect(fetcher).toHaveBeenCalledTimes(5);   // capped at 30s
    await advance(1);
    expect(fetcher).toHaveBeenCalledTimes(6);
  });

  it('starts the backoff over for the next refresh', async () => {
    const fetcher = respond(REFRESHING, REFRESHING, FRESH, REFRESHING);
    await act(async () => { render(<MemoryRouter><DashboardPage /></MemoryRouter>); });
    await advance(5000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await advance(10000);
    expect(fetcher).toHaveBeenCalledTimes(3);   // the snapshot arrives
    await advance(60000);
    expect(fetcher).toHaveBeenCalledTimes(4);   // idle poll, the next refresh starts
    await advance(5000);
    expect(fetcher).toHaveBeenCalledTimes(5);   // back to the 5s first step
  });

  it('starts the backoff over after a failed request', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => REFRESHING })
      .mockResolvedValueOnce({ ok: true, json: async () => REFRESHING })
      .mockResolvedValueOnce({ ok: false, status: 503 })
      .mockResolvedValue({ ok: true, json: async () => REFRESHING });
    vi.stubGlobal('fetch', fetcher);
    await act(async () => { render(<MemoryRouter><DashboardPage /></MemoryRouter>); });
    await advance(5000);
    await advance(10000);
    expect(fetcher).toHaveBeenCalledTimes(3);   // the 503
    await advance(60000);
    expect(fetcher).toHaveBeenCalledTimes(4);   // error retry, refreshing again
    await advance(5000);
    expect(fetcher).toHaveBeenCalledTimes(5);   // 5s, not the 20s step it had reached
  });

  it('stops polling while the tab is hidden and loads at once when it is shown', async () => {
    const fetcher = respond(FRESH);
    await act(async () => { render(<MemoryRouter><DashboardPage /></MemoryRouter>); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    hidden = true;
    await advance(10 * 60000);
    expect(fetcher).toHaveBeenCalledTimes(1);

    hidden = false;
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(fetcher).toHaveBeenCalledTimes(2);
    await advance(60000);
    expect(fetcher).toHaveBeenCalledTimes(3);   // polling resumed
  });

  it('waits to load until a tab opened in the background is shown', async () => {
    const fetcher = respond(FRESH);
    hidden = true;
    await act(async () => { render(<MemoryRouter><DashboardPage /></MemoryRouter>); });
    await advance(10 * 60000);
    expect(fetcher).not.toHaveBeenCalled();

    hidden = false;
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await advance(60000);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('does not add a load when the tab is shown before a poll was skipped', async () => {
    const fetcher = respond(FRESH);
    await act(async () => { render(<MemoryRouter><DashboardPage /></MemoryRouter>); });
    hidden = true;
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    hidden = false;
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await advance(60000);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('stops listening for visibility after unmount', async () => {
    const fetcher = respond(FRESH);
    let view!: ReturnType<typeof render>;
    await act(async () => { view = render(<MemoryRouter><DashboardPage /></MemoryRouter>); });
    hidden = true;
    await advance(60000);
    view.unmount();
    hidden = false;
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
