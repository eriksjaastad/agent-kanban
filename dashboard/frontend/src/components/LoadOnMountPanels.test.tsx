import { render, screen, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ShadowPricingPanel } from './ShadowPricingPanel';
import { Navigation } from './Navigation';

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  fetchNavigation: vi.fn(),
}));

import { fetchNavigation } from '../api';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function stubFetch(respond: (url: string) => Promise<Response>) {
  const signals: AbortSignal[] = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (init?.signal) signals.push(init.signal);
    return respond(url);
  }));
  return signals;
}

const json = (body: unknown, ok = true, status = 200) =>
  Promise.resolve({ ok, status, json: () => Promise.resolve(body) } as Response);

describe('ShadowPricingPanel', () => {
  it('loads, then renders the summary', async () => {
    stubFetch(() => json({ total_shadow_cost: 12.5, subscription_cost_monthly: 100, value_multiplier: 3, total_tokens: 2000, per_model: [], first_session_date: '', last_computed_date: '' }));
    render(<ShadowPricingPanel />);
    expect(screen.getByText('Loading token usage...')).toBeTruthy();
    expect(await screen.findByText('$12.50')).toBeTruthy();
  });

  it.each([
    ['an error field', () => json({ error: 'nope' })],
    ['a non-OK response', () => json({}, false, 500)],
    ['a network failure', () => Promise.reject(new Error('net'))],
  ])('shows unavailable on %s', async (_name, respond) => {
    stubFetch(respond);
    render(<ShadowPricingPanel />);
    expect(await screen.findByText('Shadow pricing unavailable')).toBeTruthy();
  });

  it('aborts its request on unmount', async () => {
    const signals = stubFetch(() => new Promise(() => {}));
    const { unmount } = render(<ShadowPricingPanel />);
    unmount();
    expect(signals.length).toBe(1);
    expect(signals.every(s => s.aborted)).toBe(true);
  });
});

describe('Navigation load', () => {
  it('logs a failed metadata load and keeps rendering the initial navigation', async () => {
    const log = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(fetchNavigation).mockRejectedValue(new Error('nav down'));
    render(<MemoryRouter><Navigation /></MemoryRouter>);
    await act(async () => {});
    expect(log).toHaveBeenCalledWith('Failed to load navigation metadata:', expect.any(Error));
    expect(screen.getByRole('navigation')).toBeTruthy();
  });

  it('aborts the metadata request on unmount', async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(fetchNavigation).mockImplementation((s?: AbortSignal) => { signal = s; return new Promise(() => {}); });
    const { unmount } = render(<MemoryRouter><Navigation /></MemoryRouter>);
    await act(async () => {});
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});
