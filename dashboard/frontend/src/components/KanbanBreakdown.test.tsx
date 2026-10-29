import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { KanbanBreakdown } from './KanbanBreakdown';

function entries(count: number, prefix: string) {
  return Array.from({ length: count }, (_, i) => ({
    project_id: `${prefix}-${i}`,
    project: `${prefix}-${i}`,
    count: count - i,
  }));
}

const PAYLOAD = {
  statuses: ['Backlog', 'To Do', 'In Progress', 'Review'],
  columns: {
    Backlog: [
      { project_id: 'holoapp', project: 'holoapp', count: 52 },
      { project_id: 'flo-app', project: 'flo-app', count: 30 },
    ],
    'To Do': [{ project_id: 'ai-metrics', project: 'ai-metrics', count: 4 }],
    'In Progress': [{ project_id: 'project-tracker', project: 'project-tracker', count: 2 }],
    Review: [],
  },
  totals: { Backlog: 82, 'To Do': 4, 'In Progress': 2, Review: 0 },
  generated_at: '2026-09-18T22:00:00',
};

function mockFetch(payload: unknown) {
  const fetcher = vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(payload) } as Response)
  );
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

function renderBreakdown() {
  return render(<MemoryRouter><KanbanBreakdown /></MemoryRouter>);
}

describe('KanbanBreakdown', () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('renders the four board columns with per-project counts', async () => {
    mockFetch(PAYLOAD);
    renderBreakdown();

    expect(await screen.findByText('holoapp')).toBeInTheDocument();
    for (const column of ['Backlog', 'To Do', 'In Progress', 'Review']) {
      expect(screen.getByText(column)).toBeInTheDocument();
    }
    // Column totals come from the API, not from the rows on screen.
    expect(screen.getByText('82')).toBeInTheDocument();
    expect(screen.getByText('88 open cards')).toBeInTheDocument();
    expect(screen.getByText('No cards')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'flo-app' })).toHaveAttribute('href', '/kanban/flo-app');
  });

  it('keeps each column in the order the API ranked it', async () => {
    mockFetch(PAYLOAD);
    renderBreakdown();

    await screen.findByText('holoapp');
    const backlog = screen.getByText('Backlog').closest('.breakdown-column') as HTMLElement;
    const names = within(backlog).getAllByRole('link').map(link => link.textContent);
    expect(names).toEqual(['holoapp', 'flo-app']);
  });

  it('caps long columns until every project is asked for', async () => {
    mockFetch({ ...PAYLOAD, columns: { ...PAYLOAD.columns, Backlog: entries(15, 'proj') } });
    renderBreakdown();

    await screen.findByText('proj-0');
    expect(screen.queryByText('proj-12')).not.toBeInTheDocument();
    expect(screen.getByText('+3 more')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Show every project' }));
    expect(screen.getByText('proj-14')).toBeInTheDocument();
    expect(screen.queryByText('+3 more')).not.toBeInTheDocument();
  });

  it('names a failed refresh instead of blanking the counts', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve(PAYLOAD) } as Response)
      .mockResolvedValue({ ok: false, status: 500 } as Response);
    vi.stubGlobal('fetch', fetcher);
    vi.useFakeTimers({ shouldAdvanceTime: true });

    renderBreakdown();
    await screen.findByText('holoapp');

    await vi.advanceTimersByTimeAsync(60000);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Board counts are unavailable'));
    // The last good counts survive the failure.
    expect(screen.getByText('holoapp')).toBeInTheDocument();
  });

  it('aborts its in-flight request on unmount', () => {
    const fetcher = vi.fn().mockReturnValue(new Promise(() => {}));
    vi.stubGlobal('fetch', fetcher);

    const view = renderBreakdown();
    expect(screen.getByText('Loading board counts…')).toBeInTheDocument();
    const { signal } = fetcher.mock.calls[0][1];
    view.unmount();
    expect(signal.aborted).toBe(true);
  });

  it('stays on the unavailable message, not Loading, while a retry is pending', async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new Error('down'))
      .mockReturnValue(new Promise(() => {}));
    vi.stubGlobal('fetch', fetcher);

    await act(async () => { renderBreakdown(); });
    expect(screen.getByText('Board counts are unavailable.')).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(61000); });
    expect(fetcher.mock.calls.length).toBeGreaterThan(1);
    expect(screen.queryByText('Loading board counts…')).not.toBeInTheDocument();
    expect(screen.getByText('Board counts are unavailable.')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Board counts are unavailable. Retrying shortly.');
  });
});
