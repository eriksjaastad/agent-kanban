import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ReviewHealth } from './ReviewHealth';

function mockFetch(body: unknown) {
  vi.stubGlobal('fetch', vi.fn(() =>
    Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response)));
}

describe('ReviewHealth', () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it('shows every week, including one with no recorded review', async () => {
    mockFetch({
      weeks: [
        { week_start: '2026-09-21', merged: 338, reviewed: 0, unpinned: 0, median_rounds: null, with_fail: 0, overthinking: 0 },
        { week_start: '2026-10-26', merged: 154, reviewed: 78, unpinned: 3, median_rounds: 1.5, with_fail: 10, overthinking: 2 },
      ],
      refresh_error: null,
    });
    render(<ReviewHealth />);

    expect(await screen.findByText('2026-10-26')).toBeInTheDocument();
    expect(screen.getAllByRole('listitem').map(row => row.textContent)).toEqual([
      '2026-09-210 of 338 reviewed · 0 with a FAIL · 0 called overthinking',
      '2026-10-2678 of 154 reviewed (+3 unpinned) · median 1.5 rounds · 10 with a FAIL · 2 called overthinking',
    ]);
  });

  it('says it is reading while the first collection runs, and shows a failed one', async () => {
    mockFetch({ refresh_error: null });
    const { unmount } = render(<ReviewHealth />);
    expect(await screen.findByText('Reading merged PRs…')).toBeInTheDocument();
    unmount();

    mockFetch({ refresh_error: 'Review health refresh failed. Retrying shortly.' });
    render(<ReviewHealth />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Review health refresh failed');
  });
});
