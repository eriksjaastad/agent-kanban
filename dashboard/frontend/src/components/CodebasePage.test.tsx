import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CodebasePage } from './CodebasePage';
import type { CodebaseSizeReport, CodebaseSizeRow } from '../types';

const FORMULA = 'score = (baseline_lean − current_lean) / baseline_lean × 100';

function row(over: Partial<CodebaseSizeRow>): CodebaseSizeRow {
  return {
    project: 'p', code: 0, tests: 0, doc_files: 0, doc_lines: 0, last_commit: '2026-10-01',
    commits_90d: 4, error: null, score: null, baseline_lean: null, change: null, ...over,
  };
}

const report: CodebaseSizeReport = {
  baseline_date: '2026-10-01',
  latest_date: '2026-10-08',
  formula: FORMULA,
  rows: [
    row({ project: 'alpha', code: 900, tests: 300, doc_files: 4, doc_lines: 100, change: -340, score: 25.04 }),
    row({ project: 'beta', code: 500, change: 120, score: -3.2 }),
    row({ project: 'gamma', code: 50, change: 0, score: -0.04 }),
    row({ project: 'delta', code: 10, last_commit: null, commits_90d: null }),
  ],
  total: { code: 1460, tests: 300, doc_files: 4, doc_lines: 100, repos: 4, score: 12.5 },
};

const empty: CodebaseSizeReport = {
  baseline_date: null, latest_date: null, formula: FORMULA, rows: [],
  total: { code: 0, tests: 0, doc_files: 0, doc_lines: 0, repos: 0, score: null },
};

function reply(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

afterEach(() => { vi.unstubAllGlobals(); });

describe('CodebasePage', () => {
  it('renders header, formula, rows sorted as given, and the total', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(report)));
    render(<CodebasePage />);
    expect(screen.getByRole('status')).toHaveTextContent('Loading');
    await screen.findByText('alpha');
    expect(screen.getByText('Baseline: 2026-10-01')).toBeInTheDocument();
    expect(screen.getByText('Latest run: 2026-10-08')).toBeInTheDocument();
    expect(screen.getByText(FORMULA)).toBeInTheDocument();
    const alpha = within(screen.getByText('alpha').closest('tr')!);
    expect(alpha.getByText('−340')).toBeInTheDocument();
    expect(alpha.getByText('+25.0')).toBeInTheDocument();
    const beta = within(screen.getByText('beta').closest('tr')!);
    expect(beta.getByText('+120')).toBeInTheDocument();
    expect(beta.getByText('−3.2')).toBeInTheDocument();
    const total = within(screen.getByText(/^TOTAL/).closest('tr')!);
    expect(total.getByText('1460')).toBeInTheDocument();
    expect(total.getByText('+12.5')).toBeInTheDocument();
    const names = screen.getAllByRole('rowheader').map(h => h.textContent);
    expect(names.slice(0, 4)).toEqual(['alpha', 'beta', 'gamma', 'delta']);
  });

  it('shows an em dash for null change and score, and never -0.0', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(report)));
    const { container } = render(<CodebasePage />);
    await screen.findByText('delta');
    const delta = within(screen.getByText('delta').closest('tr')!);
    expect(delta.getAllByText('—')).toHaveLength(4); // last commit, commits, change, score
    const cells = within(screen.getByText('gamma').closest('tr')!).getAllByRole('cell').map(c => c.textContent);
    expect(cells[6]).toBe('0'); // change 0
    expect(cells[7]).toBe('0.0'); // -0.04 rounds to 0.0, unsigned
    expect(container.textContent).not.toMatch(/-0\.0|−0\.0/);
  });

  it('shows the empty state when there are no runs yet', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply(empty)));
    render(<CodebasePage />);
    await screen.findByText(/No runs yet/);
    expect(screen.getByText(/it becomes the baseline/)).toBeInTheDocument();
    expect(screen.getByText('Baseline: none yet')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows a load error, not an empty table, when the load fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply({ detail: 'Table is missing' }, false, 500)));
    render(<CodebasePage />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Table is missing');
    expect(screen.queryByText(/No runs yet/)).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('replaces the table with the refresh response and disables the button while scanning', async () => {
    let finish: (value: unknown) => void = () => {};
    const fetcher = vi.fn()
      .mockResolvedValueOnce(reply(empty))
      .mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup();
    render(<CodebasePage />);
    await screen.findByText(/No runs yet/);
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(screen.getByRole('button', { name: 'Scanning…' })).toBeDisabled();
    expect(fetcher).toHaveBeenLastCalledWith('/api/codebase-size/refresh', expect.objectContaining({ method: 'POST', signal: expect.any(AbortSignal) }));
    finish(reply(report));
    await screen.findByText('alpha');
    expect(screen.queryByText(/No runs yet/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('shows the refresh error and keeps the previous rows on failure', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(reply(report))
      .mockResolvedValueOnce(reply({ detail: 'Scan failed for 1 repo(s): broken' }, false, 502));
    vi.stubGlobal('fetch', fetcher);
    const user = userEvent.setup();
    render(<CodebasePage />);
    await screen.findByText('alpha');
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Scan failed for 1 repo(s): broken'));
    expect(screen.getByText('alpha')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('serializes requests: Refresh is disabled while the initial load is in flight', async () => {
    let resolveGet: (value: unknown) => void = () => {};
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => new Promise(resolve => {
      if (init?.method === 'POST') resolve(reply(report));
      else resolveGet = resolve;
    }));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<CodebasePage />);
    const refresh = screen.getByRole('button', { name: 'Refresh' });
    expect(refresh).toBeDisabled();
    await user.click(refresh);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
    resolveGet(reply(report));
    await screen.findByText('alpha');
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('serializes requests: Retry is disabled while a refresh runs, and the refresh result is shown', async () => {
    let resolvePost: (value: unknown) => void = () => {};
    const fresh: CodebaseSizeReport = { ...report, latest_date: '2026-10-09', rows: [row({ project: 'newest', code: 7 })] };
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return new Promise(resolve => { resolvePost = resolve; });
      return Promise.resolve(reply({ detail: 'database locked' }, false, 500));
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<CodebasePage />);
    const retry = await screen.findByRole('button', { name: 'Retry' });
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(screen.getByRole('button', { name: 'Scanning…' })).toBeDisabled();
    expect(retry).toBeDisabled();
    await user.click(retry);  // must not start a GET that would supersede the refresh
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method !== 'POST')).toHaveLength(1);
    resolvePost(reply(fresh));
    await screen.findByText('newest');
    expect(screen.getByText('Latest run: 2026-10-09')).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/)).not.toBeInTheDocument();
  });

  it('a double click on Refresh sends one scan', async () => {
    let resolvePost: (value: unknown) => void = () => {};
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return new Promise(resolve => { resolvePost = resolve; });
      return Promise.resolve(reply(report));
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<CodebasePage />);
    await screen.findByText('alpha');
    await user.dblClick(screen.getByRole('button', { name: 'Refresh' }));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    resolvePost(reply(report));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled());
  });

  describe('timeouts', () => {
    afterEach(() => { vi.useRealTimers(); });

    function hang(init?: RequestInit) {
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' })));
      });
    }

    it('a load that never answers becomes a load error and frees the buttons', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => hang(init)));
      render(<CodebasePage />);
      expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(await screen.findByText(/Could not load codebase size: no answer after 30s/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
    });

    it('a refresh that never answers reports it and frees the buttons, keeping the rows', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) =>
        init?.method === 'POST' ? hang(init) : Promise.resolve(reply(report))));
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      render(<CodebasePage />);
      await screen.findByText('alpha');
      await user.click(screen.getByRole('button', { name: 'Refresh' }));
      expect(screen.getByRole('button', { name: 'Scanning…' })).toBeDisabled();
      await act(async () => { await vi.advanceTimersByTimeAsync(180_000); });
      expect(await screen.findByText(/no answer after 3 minutes/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
      expect(screen.getByText('alpha')).toBeInTheDocument();
    });
  });

  it('a successful Retry clears a refresh error that described older data', async () => {
    let getCalls = 0;
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return Promise.resolve(reply({ detail: 'Scan failed for 1 repo(s)' }, false, 502));
      getCalls += 1;
      return Promise.resolve(getCalls === 1 ? reply({ detail: 'database locked' }, false, 500) : reply(report));
    }));
    const user = userEvent.setup();
    render(<CodebasePage />);
    await screen.findByRole('button', { name: 'Retry' });
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/Refresh failed: Scan failed/);
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('alpha');
    expect(screen.queryByText(/Refresh failed/)).not.toBeInTheDocument();
  });
});
