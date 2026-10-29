import { act, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';

import { VolunteerPage } from './VolunteerPage';

globalThis.fetch = vi.fn() as typeof fetch;

vi.mock('./PageShell', () => ({
  PageShell: ({ children, title }: { children: React.ReactNode; title: string }) => (
    <div>
      <h1>{title}</h1>
      {children}
    </div>
  ),
}));

vi.mock('./Spinner', () => ({
  Spinner: () => <div>Loading...</div>,
}));

vi.mock('./Notification', () => ({
  Notification: ({ message }: { message: string }) => <div role="alert">{message}</div>,
}));

function role(id: number, extra: Record<string, unknown> = {}) {
  return {
    id,
    company: `Org ${id}`,
    title: `Role ${id}`,
    url: `https://example.org/forms/${id}`,
    location: 'Remote',
    posted_date: null,
    first_seen: '2026-10-01',
    volunteering_since: null,
    ...extra,
  };
}

function respond(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 500, json: async () => body } as Response;
}

function section(name: string) {
  return within(screen.getByRole('region', { name }));
}

describe('VolunteerPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('renders the three sections with direct links that open in a new tab', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [role(1, { volunteering_since: '2026-09-01T00:00:00+00:00' })],
      applied: [role(2, { last_submitted_at: '2026-10-02T12:00:00+00:00' })],
      possible: [role(3)],
      possible_stale: [],
    }));

    render(<VolunteerPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });
    expect(fetch).toHaveBeenCalledWith('/api/volunteer', expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(section('Currently volunteering').getByText('Role 1')).toBeInTheDocument();
    expect(section('Currently volunteering').getByText('since 2026-09-01')).toBeInTheDocument();
    expect(section('Applied').getByText('applied 2026-10-02')).toBeInTheDocument();
    const link = section('Possible').getByRole('link', { name: 'Role 3' });
    expect(link).toHaveAttribute('href', 'https://example.org/forms/3');
    expect(link).toHaveAttribute('target', '_blank');
    expect(section('Possible').getByLabelText('Dismiss Role 3')).toBeInTheDocument();
  });

  it('dismisses a possible listing through the Jobs soft delete', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(3), role(4)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 3')).toBeInTheDocument());

    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({ job: { id: 3 } }))
      .mockResolvedValueOnce(respond({ current: [], applied: [], possible: [role(4)], possible_stale: [] }));
    await user.click(screen.getByLabelText('Dismiss Role 3'));

    expect(fetch).toHaveBeenCalledWith('/api/jobs/3', { method: 'DELETE' });
    await waitFor(() => expect(screen.queryByText('Role 3')).not.toBeInTheDocument());
    expect(screen.getByText('Role 4')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Listing dismissed');
  });

  it('keeps the listing when the dismiss fails', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(3)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 3')).toBeInTheDocument());

    vi.mocked(fetch).mockResolvedValueOnce(respond({}, false));
    await user.click(screen.getByLabelText('Dismiss Role 3'));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Failed to dismiss listing'));
    expect(screen.getByText('Role 3')).toBeInTheDocument();
  });

  it('moves a possible role to Applied, then to Currently volunteering, as the server reports', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(5)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 5')).toBeInTheDocument());

    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({ submission: { id: 1, job_id: 5 } }, true))
      .mockResolvedValueOnce(respond({
        current: [], applied: [role(5, { last_submitted_at: '2026-10-05T09:00:00+00:00' })],
        possible: [], possible_stale: [],
      }));
    await user.click(screen.getByLabelText('Mark Role 5 as submitted'));
    expect(fetch).toHaveBeenCalledWith('/api/jobs/5/submissions', expect.objectContaining({ method: 'POST' }));
    await waitFor(() => expect(section('Applied').getByText('applied 2026-10-05')).toBeInTheDocument());
    expect(section('Possible').queryByText('Role 5')).not.toBeInTheDocument();

    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({ job: { id: 5 } }))
      .mockResolvedValueOnce(respond({
        current: [role(5, { volunteering_since: '2026-10-06T10:00:00+00:00' })],
        applied: [], possible: [], possible_stale: [],
      }));
    await user.click(screen.getByLabelText('Started volunteering at Org 5'));
    expect(fetch).toHaveBeenCalledWith('/api/jobs/5/volunteering', { method: 'POST' });
    await waitFor(() => expect(section('Currently volunteering').getByText('since 2026-10-06')).toBeInTheDocument());
    expect(section('Applied').queryByText('Role 5')).not.toBeInTheDocument();
  });

  it('shows a role where the server keeps it, not where this page would guess', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(9)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 9')).toBeInTheDocument());

    // Another tab already started this role on 2026-09-15. Submitting here
    // records an application, but the server still lists the role as current.
    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({ submission: { id: 4, job_id: 9 } }))
      .mockResolvedValueOnce(respond({
        current: [role(9, { volunteering_since: '2026-09-15T08:00:00+00:00' })],
        applied: [], possible: [], possible_stale: [],
      }));
    await user.click(screen.getByLabelText('Mark Role 9 as submitted'));

    await waitFor(() => expect(section('Currently volunteering').getByText('since 2026-09-15')).toBeInTheDocument());
    expect(section('Applied').queryByText('Role 9')).not.toBeInTheDocument();
  });

  it('keeps a role locked until the reload after its write lands', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(10)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 10')).toBeInTheDocument());

    let finishReload: (value: Response) => void = () => {};
    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({ submission: { id: 5, job_id: 10 } }))
      .mockReturnValueOnce(new Promise<Response>(resolve => { finishReload = resolve; }));
    await user.click(screen.getByLabelText('Mark Role 10 as submitted'));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Marked as applied'));

    // The write is done and the row still shows, but a second click must
    // not post a duplicate submission.
    expect(screen.getByLabelText('Mark Role 10 as submitted')).toBeDisabled();
    await user.click(screen.getByLabelText('Mark Role 10 as submitted'));
    expect(vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/jobs/10/submissions')).toHaveLength(1);

    finishReload(respond({
      current: [], applied: [role(10, { last_submitted_at: '2026-10-07T00:00:00+00:00' })],
      possible: [], possible_stale: [],
    }));
    await waitFor(() => expect(section('Applied').getByText('Role 10')).toBeInTheDocument());
  });

  it('locks the rows while a failed reload leaves them possibly stale, until Retry succeeds', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(11), role(12)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 11')).toBeInTheDocument());

    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({ submission: { id: 9, job_id: 11 } }))
      .mockResolvedValueOnce(respond({}, false));
    await user.click(screen.getByLabelText('Mark Role 11 as submitted'));

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(
      'Could not refresh volunteer roles; this list may be out of date.'));
    // Role 11 is already submitted on the server but still shows here: no
    // second click may post a duplicate, and no other row acts on stale data.
    expect(section('Possible').getByText('Role 11')).toBeInTheDocument();
    expect(screen.getByLabelText('Mark Role 11 as submitted')).toBeDisabled();
    expect(screen.getByLabelText('Dismiss Role 12')).toBeDisabled();
    await user.click(screen.getByLabelText('Mark Role 11 as submitted'));
    expect(vi.mocked(fetch).mock.calls.filter(([u]) => u === '/api/jobs/11/submissions')).toHaveLength(1);

    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [role(11)], possible: [role(12)], possible_stale: [],
    }));
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(section('Applied').getByText('Role 11')).toBeInTheDocument());
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Dismiss Role 12')).toBeEnabled();
  });

  it('claims nothing about the sections when the first load fails', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(respond({}, false));
    render(<VolunteerPage />);

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Could not load volunteer roles.'));
    expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    expect(screen.queryByText('Not volunteering anywhere yet')).not.toBeInTheDocument();
    expect(screen.queryByText('No applications yet')).not.toBeInTheDocument();
    expect(screen.queryByText('No open volunteer listings')).not.toBeInTheDocument();

    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(13)], possible_stale: [],
    }));
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(section('Possible').getByText('Role 13')).toBeInTheDocument());
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it("keeps a role locked through its own reload even when another role's reload lands first", async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(20), role(21)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 20')).toBeInTheDocument());

    let finishWriteA: (value: Response) => void = () => {};
    let finishReloadA: (value: Response) => void = () => {};
    const afterB = { current: [], applied: [role(21)], possible: [role(20)], possible_stale: [] };
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input);
      const method = (init as RequestInit | undefined)?.method;
      if (url === '/api/jobs/20/submissions') return new Promise<Response>(r => { finishWriteA = r; });
      if (url === '/api/jobs/21/submissions') return respond({ submission: { id: 7, job_id: 21 } });
      if (url === '/api/volunteer' && method === undefined) {
        // The first reload (after B) lands at once; the one after A waits.
        if (vi.mocked(fetch).mock.calls.filter(([u]) => u === '/api/volunteer').length <= 2) return respond(afterB);
        return new Promise<Response>(r => { finishReloadA = r; });
      }
      throw new Error(`unexpected ${url}`);
    });

    await user.click(screen.getByLabelText('Mark Role 20 as submitted'));   // A: write pending
    await user.click(screen.getByLabelText('Mark Role 21 as submitted'));   // B: write + reload land
    await waitFor(() => expect(section('Applied').getByText('Role 21')).toBeInTheDocument());

    finishWriteA(respond({ submission: { id: 8, job_id: 20 } }));           // A's reload now pending
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Marked as applied'));
    await waitFor(() => expect(screen.getByLabelText('Mark Role 20 as submitted')).toBeDisabled());
    await user.click(screen.getByLabelText('Mark Role 20 as submitted'));
    expect(vi.mocked(fetch).mock.calls.filter(([u]) => u === '/api/jobs/20/submissions')).toHaveLength(1);

    finishReloadA(respond({ current: [], applied: [role(20), role(21)], possible: [], possible_stale: [] }));
    await waitFor(() => expect(section('Applied').getByText('Role 20')).toBeInTheDocument());
  });

  it('gives up on a hung load after 15 seconds so nothing stays locked', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(fetch).mockReturnValue(new Promise(() => {}));
      render(<VolunteerPage />);
      await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
      expect(screen.getByRole('status')).toHaveTextContent('Could not load volunteer roles.');
    } finally {
      vi.useRealTimers();
    }
  });

  it('aborts its load on unmount', () => {
    vi.mocked(fetch).mockReturnValue(new Promise(() => {}));
    const view = render(<VolunteerPage />);
    const [[, init]] = vi.mocked(fetch).mock.calls;
    const signal = (init as RequestInit).signal as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
  });

  it('limits Possible to the top 25 and hides stale listings behind a count', async () => {
    const user = userEvent.setup();
    const possible = Array.from({ length: 27 }, (_, i) => role(100 + i));
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible, possible_stale: [role(900)],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 100')).toBeInTheDocument());

    expect(screen.getByText('Role 124')).toBeInTheDocument();
    expect(screen.queryByText('Role 125')).not.toBeInTheDocument();
    expect(screen.getByText('1 older listing hidden')).toBeInTheDocument();
    expect(screen.queryByText('Role 900')).not.toBeInTheDocument();

    // Older listings appear on their own, even while the top-25 limit holds.
    await user.click(screen.getByRole('button', { name: 'Show older listings' }));
    expect(screen.getByRole('region', { name: 'Older volunteer listings' })).toHaveTextContent('Role 900');
    expect(screen.queryByText('Role 125')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Show all 27' }));
    expect(screen.getByText('Role 126')).toBeInTheDocument();
  });

  it('moves a possible role straight to Currently volunteering', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(7)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 7')).toBeInTheDocument());

    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({ job: { id: 7 } }))
      .mockResolvedValueOnce(respond({
        current: [role(7, { volunteering_since: '2026-10-08T00:00:00+00:00' })],
        applied: [], possible: [], possible_stale: [],
      }));
    await user.click(section('Possible').getByLabelText('Started volunteering at Org 7'));

    expect(fetch).toHaveBeenCalledWith('/api/jobs/7/volunteering', { method: 'POST' });
    await waitFor(() => expect(section('Currently volunteering').getByText('Role 7')).toBeInTheDocument());
    expect(section('Possible').queryByText('Role 7')).not.toBeInTheDocument();
    expect(section('Applied').queryByText('Role 7')).not.toBeInTheDocument();
  });

  it('lists Possible roles in server rank order, organisation shown on each row', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [],
      possible: [
        role(21, { company: 'Acme' }), role(22, { company: 'Beta' }), role(23, { company: 'Acme' }),
      ],
      possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 21')).toBeInTheDocument());

    const titles = section('Possible').getAllByRole('link').map(link => link.textContent);
    expect(titles).toEqual(['Role 21', 'Role 22', 'Role 23']);
    expect(section('Possible').getAllByText('Acme')).toHaveLength(2);
  });

  it('shows the 409 migration message and leaves the role in place', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      current: [], applied: [], possible: [role(8)], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('Role 8')).toBeInTheDocument());

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: 'volunteer roles need migration 016; run `pt db migrate`' }),
    } as Response);
    await user.click(section('Possible').getByLabelText('Started volunteering at Org 8'));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('run `pt db migrate`'));
    expect(section('Possible').getByText('Role 8')).toBeInTheDocument();
    expect(section('Currently volunteering').queryByText('Role 8')).not.toBeInTheDocument();
  });

  it('says when the database still needs migration 016', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(respond({
      migration_pending: true, current: [], applied: [], possible: [], possible_stale: [],
    }));
    render(<VolunteerPage />);
    await waitFor(() => expect(screen.getByText('pt db migrate')).toBeInTheDocument());
  });

});
