import { render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';

import { JobsPage } from './JobsPage';

globalThis.fetch = vi.fn() as typeof fetch;

vi.mock('./PageShell', () => ({
  PageShell: ({ children, title, subtitle }: { children: React.ReactNode; title: string; subtitle?: string }) => (
    <div>
      <h1>{title}</h1>
      {subtitle && <p>{subtitle}</p>}
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

vi.mock('./JobStatsChart', () => ({
  JobStatsChart: () => <div data-testid="job-stats-chart">Job Stats Chart</div>,
}));

vi.mock('./CategoryTable', () => ({
  CategoryTable: ({ categories }: { categories: Array<{ category: string; count: number }> }) => (
    <div data-testid="category-table">
      {categories.map(cat => (
        <span key={cat.category} data-testid={`category-count-${cat.category}`}>
          {cat.category}: {cat.count}
        </span>
      ))}
    </div>
  ),
}));

// Creates a promise this test controls the resolution of, so it can
// interleave two in-flight requests in a specific order.
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

describe('JobsPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('lists jobs flat in server rank order, never regrouped by company', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 1,
            company: 'Acme Corp',
            title: 'Frontend Developer',
            url: 'https://example.com/job1',
            location: 'San Francisco',
            posted_date: '2026-09-20',
            first_seen: '2026-09-22',
            category: 'Frontend/React',
            source: 'ats_sweep',
            deleted_at: null,
            raw: null,
          },
          {
            id: 3,
            company: 'Tech Inc',
            title: 'Full Stack Developer',
            url: 'https://example.com/job3',
            location: 'New York',
            posted_date: '2026-09-19',
            first_seen: '2026-09-21',
            category: 'Full Stack',
            source: 'hn',
            deleted_at: null,
            raw: null,
          },
          {
            id: 2,
            company: 'Acme Corp',
            title: 'Backend Engineer',
            url: 'https://example.com/job2',
            location: 'Remote',
            posted_date: '2026-09-21',
            first_seen: '2026-09-23',
            category: 'Backend',
            source: 'ats_sweep',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    // Server rank: Acme (best), Tech Inc, then Acme's weaker role. Grouping
    // by company would have pulled Backend Engineer above Full Stack.
    const titles = screen.getAllByRole('link').map(link => link.textContent);
    expect(titles).toEqual(['Frontend Developer', 'Full Stack Developer', 'Backend Engineer']);
    expect(screen.getAllByText('Acme Corp')).toHaveLength(2);
    expect(screen.getAllByText('Tech Inc')).toHaveLength(1);
  });

  it('deletes a job and removes it from view', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 1,
            company: 'Acme Corp',
            title: 'Frontend Developer',
            url: 'https://example.com/job1',
            location: 'San Francisco',
            posted_date: '2026-09-20',
            first_seen: '2026-09-22',
            category: 'Frontend/React',
            source: 'ats_sweep',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Frontend Developer')).toBeInTheDocument();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ job: { id: 1, deleted_at: '2026-09-24T00:00:00Z' } }),
    } as Response);

    const deleteButton = screen.getByLabelText('Dismiss Frontend Developer');
    await user.click(deleteButton);

    await waitFor(() => {
      expect(screen.queryByText('Frontend Developer')).not.toBeInTheDocument();
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Job dismissed');
  });

  it('shows empty state when no jobs', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jobs: [] }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('No open job listings')).toBeInTheDocument();
  });

  it('sets deleted_at via DELETE endpoint', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 5,
            company: 'Test Corp',
            title: 'Test Job',
            url: 'https://example.com/test',
            location: null,
            posted_date: null,
            first_seen: '2026-09-24',
            category: 'Other',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Test Job')).toBeInTheDocument();
    });

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        job: { id: 5, deleted_at: '2026-09-24T12:00:00Z' },
      }),
    } as Response);

    const deleteButton = screen.getByLabelText('Dismiss Test Job');
    await user.click(deleteButton);

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/jobs/5', { method: 'DELETE' });
    });

    expect(screen.queryByText('Test Job')).not.toBeInTheDocument();
  });

  it('shows error notification when jobs fail to load', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load jobs');
  });

  it('shows error notification and keeps job in list when delete fails', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 7,
            company: 'Error Corp',
            title: 'Undeletable Job',
            url: 'https://example.com/job',
            location: 'Remote',
            posted_date: '2026-09-23',
            first_seen: '2026-09-24',
            category: 'Backend',
            source: 'ats_sweep',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Undeletable Job')).toBeInTheDocument();
    });

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    const deleteButton = screen.getByLabelText('Dismiss Undeletable Job');
    await user.click(deleteButton);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Failed to dismiss job');
    });

    expect(screen.getByText('Undeletable Job')).toBeInTheDocument();
  });

  it('loads and displays job stats chart when stats are available', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ jobs: [] }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          jobs_per_day: [{ date: '2026-09-20', count: 5 }],
          submissions_per_day: [{ date: '2026-09-21', count: 2 }],
          categories: [{ category: 'Frontend/React', count: 3 }],
        }),
      } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByTestId('job-stats-chart')).toBeInTheDocument();
      expect(screen.getByTestId('category-table')).toBeInTheDocument();
    });
  });

  it('handles stats loading failure gracefully', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ jobs: [] }),
      } as Response)
      .mockRejectedValueOnce(new Error('Stats unavailable'));

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.queryByTestId('job-stats-chart')).not.toBeInTheDocument();
      expect(screen.queryByTestId('category-table')).not.toBeInTheDocument();
    });
  });

  it('queues agent prompt and shows success notification', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 10,
            company: 'Test Inc',
            title: 'Senior Engineer',
            url: 'https://example.com/job10',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Senior Engineer')).toBeInTheDocument();
    });

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        success: true,
        job_id: 10,
        message: 'Agent prompt queued successfully',
      }),
    } as Response);

    const agentButton = screen.getByLabelText('Queue agent prompt for Senior Engineer');
    await user.click(agentButton);

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/jobs/10/agent-prompt', { method: 'POST' });
    });

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Agent prompt queued successfully');
    });
  });

  it('shows error when agent prompt queueing fails', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 11,
            company: 'Error Inc',
            title: 'Test Job',
            url: 'https://example.com/job11',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Other',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Test Job')).toBeInTheDocument();
    });

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'Failed to queue agent prompt: Agent not available' }),
    } as Response);

    const agentButton = screen.getByLabelText('Queue agent prompt for Test Job');
    await user.click(agentButton);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Failed to queue agent prompt: Agent not available');
    });
  });

  it('submits a job and removes it from view', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 20,
            company: 'Submit Corp',
            title: 'Submittable Job',
            url: 'https://example.com/job20',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Submittable Job')).toBeInTheDocument();
    });

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        submission: { id: 1, job_id: 20, submitted_at: '2026-09-25T12:00:00+00:00' },
      }),
    } as Response);

    const submitButton = screen.getByLabelText('Mark Submittable Job as submitted');
    await user.click(submitButton);

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/jobs/20/submissions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
    });

    await waitFor(() => {
      expect(screen.queryByText('Submittable Job')).not.toBeInTheDocument();
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Job marked as submitted');
  });

  it('sends exactly one POST when the submit button is double-clicked', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 21,
            company: 'Double Click Inc',
            title: 'Fast Clicker Job',
            url: 'https://example.com/job21',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Fast Clicker Job')).toBeInTheDocument();
    });

    let resolveSubmit: (value: Response) => void = () => {};
    vi.mocked(fetch).mockImplementationOnce(
      () =>
        new Promise<Response>(resolve => {
          resolveSubmit = resolve;
        })
    );

    const submitButton = screen.getByLabelText('Mark Fast Clicker Job as submitted');
    await user.dblClick(submitButton);

    expect(submitButton).toBeDisabled();

    resolveSubmit({
      ok: true,
      json: async () => ({
        submission: { id: 2, job_id: 21, submitted_at: '2026-09-25T12:00:00+00:00' },
      }),
    } as Response);

    await waitFor(() => {
      expect(screen.queryByText('Fast Clicker Job')).not.toBeInTheDocument();
    });

    const submissionPosts = vi
      .mocked(fetch)
      .mock.calls.filter(([url]) => url === '/api/jobs/21/submissions');
    expect(submissionPosts).toHaveLength(1);
  });

  it('shows error notification and keeps job in list when submit fails', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 22,
            company: 'Flaky Corp',
            title: 'Unsubmittable Job',
            url: 'https://example.com/job22',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Unsubmittable Job')).toBeInTheDocument();
    });

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    const submitButton = screen.getByLabelText('Mark Unsubmittable Job as submitted');
    await user.click(submitButton);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Failed to submit job');
    });

    expect(screen.getByText('Unsubmittable Job')).toBeInTheDocument();
    expect(submitButton).not.toBeDisabled();
  });

  it('removes both rows when two submits overlap and resolve out of order', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 30,
            company: 'Overlap Inc',
            title: 'Job A',
            url: 'https://example.com/jobA',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
          {
            id: 31,
            company: 'Overlap Inc',
            title: 'Job B',
            url: 'https://example.com/jobB',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Job A')).toBeInTheDocument();
      expect(screen.getByText('Job B')).toBeInTheDocument();
    });

    const submitA = deferred<Response>();
    const submitB = deferred<Response>();
    const statsRefresh = { ok: true, json: async () => ({ jobs_per_day: [], submissions_per_day: [], categories: [] }) } as Response;

    vi.mocked(fetch).mockImplementation((url: string | URL | Request) => {
      if (url === '/api/jobs/30/submissions') return submitA.promise;
      if (url === '/api/jobs/31/submissions') return submitB.promise;
      if (url === '/api/jobs/stats') return Promise.resolve(statsRefresh);
      throw new Error(`Unexpected fetch call: ${url}`);
    });

    await user.click(screen.getByLabelText('Mark Job A as submitted'));
    await user.click(screen.getByLabelText('Mark Job B as submitted'));

    // Resolve the second submission first, then the first — the opposite
    // of request order — to prove removal doesn't depend on resolve order.
    submitB.resolve({
      ok: true,
      json: async () => ({ submission: { id: 2, job_id: 31 } }),
    } as Response);

    await waitFor(() => {
      expect(screen.queryByText('Job B')).not.toBeInTheDocument();
    });
    expect(screen.getByText('Job A')).toBeInTheDocument();

    submitA.resolve({
      ok: true,
      json: async () => ({ submission: { id: 1, job_id: 30 } }),
    } as Response);

    await waitFor(() => {
      expect(screen.queryByText('Job A')).not.toBeInTheDocument();
    });
    expect(screen.queryByText('Job B')).not.toBeInTheDocument();
  });

  it('removes both rows when a submit and a delete overlap on different jobs', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 32,
            company: 'Mixed Co',
            title: 'Job To Submit',
            url: 'https://example.com/jobSubmit',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
          {
            id: 33,
            company: 'Mixed Co',
            title: 'Job To Delete',
            url: 'https://example.com/jobDelete',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Job To Submit')).toBeInTheDocument();
      expect(screen.getByText('Job To Delete')).toBeInTheDocument();
    });

    const submitReq = deferred<Response>();
    const deleteReq = deferred<Response>();
    const statsRefresh = { ok: true, json: async () => ({ jobs_per_day: [], submissions_per_day: [], categories: [] }) } as Response;

    vi.mocked(fetch).mockImplementation((url: string | URL | Request, options?: RequestInit) => {
      if (url === '/api/jobs/32/submissions') return submitReq.promise;
      if (url === '/api/jobs/33' && options?.method === 'DELETE') return deleteReq.promise;
      if (url === '/api/jobs/stats') return Promise.resolve(statsRefresh);
      throw new Error(`Unexpected fetch call: ${url}`);
    });

    await user.click(screen.getByLabelText('Mark Job To Submit as submitted'));
    await user.click(screen.getByLabelText('Dismiss Job To Delete'));

    // The delete (issued second) resolves first.
    deleteReq.resolve({
      ok: true,
      json: async () => ({ job: { id: 33, deleted_at: '2026-09-26T00:00:00Z' } }),
    } as Response);

    await waitFor(() => {
      expect(screen.queryByText('Job To Delete')).not.toBeInTheDocument();
    });
    expect(screen.getByText('Job To Submit')).toBeInTheDocument();

    submitReq.resolve({
      ok: true,
      json: async () => ({ submission: { id: 3, job_id: 32 } }),
    } as Response);

    await waitFor(() => {
      expect(screen.queryByText('Job To Submit')).not.toBeInTheDocument();
    });
    expect(screen.queryByText('Job To Delete')).not.toBeInTheDocument();
  });

  it('refetches stats after a successful submit and the displayed count changes', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          jobs: [
            {
              id: 40,
              company: 'Stats Co',
              title: 'Countable Job',
              url: 'https://example.com/job40',
              location: 'Remote',
              posted_date: '2026-09-25',
              first_seen: '2026-09-25',
              category: 'Backend',
              source: 'manual',
              deleted_at: null,
              raw: null,
            },
          ],
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          jobs_per_day: [],
          submissions_per_day: [],
          categories: [{ category: 'Backend', count: 5 }],
        }),
      } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByTestId('category-count-Backend')).toHaveTextContent('Backend: 5');
    });

    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ submission: { id: 5, job_id: 40 } }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          jobs_per_day: [],
          submissions_per_day: [{ date: '2026-09-25', count: 1 }],
          categories: [{ category: 'Backend', count: 4 }],
        }),
      } as Response);

    await user.click(screen.getByLabelText('Mark Countable Job as submitted'));

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/jobs/stats', { signal: expect.any(AbortSignal) });
    });

    await waitFor(() => {
      expect(screen.getByTestId('category-count-Backend')).toHaveTextContent('Backend: 4');
    });

    const statsCalls = vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/jobs/stats');
    expect(statsCalls).toHaveLength(2);
  });

  it('discards a stale stats response that resolves after a newer refresh', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          jobs: [
            {
              id: 41,
              company: 'Race Co',
              title: 'Job One',
              url: 'https://example.com/job41',
              location: 'Remote',
              posted_date: '2026-09-25',
              first_seen: '2026-09-25',
              category: 'Backend',
              source: 'manual',
              deleted_at: null,
              raw: null,
            },
            {
              id: 42,
              company: 'Race Co',
              title: 'Job Two',
              url: 'https://example.com/job42',
              location: 'Remote',
              posted_date: '2026-09-25',
              first_seen: '2026-09-25',
              category: 'Backend',
              source: 'manual',
              deleted_at: null,
              raw: null,
            },
          ],
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          jobs_per_day: [],
          submissions_per_day: [],
          categories: [{ category: 'Backend', count: 9 }],
        }),
      } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByTestId('category-count-Backend')).toHaveTextContent('Backend: 9');
    });

    const submitOne = deferred<Response>();
    const submitTwo = deferred<Response>();
    const staleStats = deferred<Response>();
    const freshStats = deferred<Response>();
    let statsCallCount = 0;

    vi.mocked(fetch).mockImplementation((url: string | URL | Request) => {
      if (url === '/api/jobs/41/submissions') return submitOne.promise;
      if (url === '/api/jobs/42/submissions') return submitTwo.promise;
      if (url === '/api/jobs/stats') {
        statsCallCount += 1;
        return statsCallCount === 1 ? staleStats.promise : freshStats.promise;
      }
      throw new Error(`Unexpected fetch call: ${url}`);
    });

    await user.click(screen.getByLabelText('Mark Job One as submitted'));
    submitOne.resolve({
      ok: true,
      json: async () => ({ submission: { id: 10, job_id: 41 } }),
    } as Response);
    // Let handleSubmit's continuation run far enough to issue the first
    // (older) stats refresh before the second submit issues the next one.
    await waitFor(() => expect(statsCallCount).toBe(1));

    await user.click(screen.getByLabelText('Mark Job Two as submitted'));
    submitTwo.resolve({
      ok: true,
      json: async () => ({ submission: { id: 11, job_id: 42 } }),
    } as Response);
    await waitFor(() => expect(statsCallCount).toBe(2));

    // The newer (second) request resolves first with the fresh count...
    freshStats.resolve({
      ok: true,
      json: async () => ({
        jobs_per_day: [],
        submissions_per_day: [],
        categories: [{ category: 'Backend', count: 7 }],
      }),
    } as Response);

    await waitFor(() => {
      expect(screen.getByTestId('category-count-Backend')).toHaveTextContent('Backend: 7');
    });

    // ...and the older request's stale response arrives late. It must not
    // overwrite the fresher count that already landed.
    staleStats.resolve({
      ok: true,
      json: async () => ({
        jobs_per_day: [],
        submissions_per_day: [],
        categories: [{ category: 'Backend', count: 999 }],
      }),
    } as Response);

    // Give the stale response's continuation a chance to run, then assert
    // it did not win.
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(screen.getByTestId('category-count-Backend')).toHaveTextContent('Backend: 7');
  });

  it('disables agent button while queueing prompt', async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            id: 12,
            company: 'Test Corp',
            title: 'Engineer',
            url: 'https://example.com/job12',
            location: 'Remote',
            posted_date: '2026-09-25',
            first_seen: '2026-09-25',
            category: 'Backend',
            source: 'manual',
            deleted_at: null,
            raw: null,
          },
        ],
      }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Engineer')).toBeInTheDocument();
    });

    vi.mocked(fetch).mockImplementationOnce(() => 
      new Promise(resolve => setTimeout(() => 
        resolve({
          ok: true,
          json: async () => ({ success: true, job_id: 12, message: 'Queued' }),
        } as Response),
        100
      ))
    );

    const agentButton = screen.getByLabelText('Queue agent prompt for Engineer');
    expect(agentButton).not.toBeDisabled();

    await user.click(agentButton);

    expect(agentButton).toBeDisabled();
    expect(agentButton).toHaveTextContent('...');

    await waitFor(() => {
      expect(agentButton).not.toBeDisabled();
      expect(agentButton).toHaveTextContent('🤖');
    });
  });

  function rankedJob(id: number, company = `Company ${id}`) {
    return {
      id,
      company,
      title: `Role ${id}`,
      url: `https://example.com/ranked/${id}`,
      location: 'Remote',
      posted_date: null,
      first_seen: '2026-10-01',
      category: 'Other',
      source: 'ats_sweep',
      deleted_at: null,
      raw: null,
    };
  }

  it('shows the top 25 in server rank order and a Show all control', async () => {
    const user = userEvent.setup();
    const fresh = Array.from({ length: 30 }, (_, i) => rankedJob(i + 1));
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jobs: fresh, stale: [] }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Role 1')).toBeInTheDocument();
    });
    expect(screen.getByText('Role 25')).toBeInTheDocument();
    expect(screen.queryByText('Role 26')).not.toBeInTheDocument();
    const titles = screen.getAllByRole('link').map(link => link.textContent);
    expect(titles.slice(0, 3)).toEqual(['Role 1', 'Role 2', 'Role 3']);
    expect(screen.queryByText(/older listing/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Show all 30' }));
    expect(screen.getByText('Role 30')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Show all/ })).not.toBeInTheDocument();
  });

  it('hides stale listings behind a count until asked', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jobs: [rankedJob(1)], stale: [rankedJob(2), rankedJob(3)] }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('Role 1')).toBeInTheDocument();
    });
    expect(screen.getByText('2 older listings hidden')).toBeInTheDocument();
    expect(screen.queryByText('Role 2')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Show older listings' }));
    expect(screen.getByText('Role 2')).toBeInTheDocument();
    expect(screen.getByText('Role 3')).toBeInTheDocument();
    expect(screen.queryByText('2 older listings hidden')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Hide older listings' }));
    expect(screen.queryByText('Role 2')).not.toBeInTheDocument();
  });

  it('reveals older listings even when 25 or more fresh listings fill the limit', async () => {
    const user = userEvent.setup();
    const fresh = Array.from({ length: 30 }, (_, i) => rankedJob(i + 1));
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jobs: fresh, stale: [rankedJob(91), rankedJob(92)] }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('2 older listings hidden')).toBeInTheDocument();
    });
    await user.click(screen.getByRole('button', { name: 'Show older listings' }));
    const older = screen.getByRole('region', { name: 'Older listings' });
    expect(older).toHaveTextContent('Role 91');
    expect(older).toHaveTextContent('Role 92');
    // The fresh list is still limited to its top 25.
    expect(screen.queryByText('Role 26')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show all 30' })).toBeInTheDocument();
  });

  it('keeps older listings in server rank order across companies', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [],
        stale: [rankedJob(11, 'Acme'), rankedJob(12, 'Beta'), rankedJob(13, 'Acme')],
      }),
    } as Response);

    render(<JobsPage />);

    await user.click(await screen.findByRole('button', { name: 'Show older listings' }));
    const older = screen.getByRole('region', { name: 'Older listings' });
    const titles = within(older).getAllByRole('link').map(link => link.textContent);
    expect(titles).toEqual(['Role 11', 'Role 12', 'Role 13']);
  });

  it('dismisses and submits rows from the older-listings section', async () => {
    const user = userEvent.setup();
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jobs: [rankedJob(1)], stale: [rankedJob(31), rankedJob(32)] }),
    } as Response);

    render(<JobsPage />);

    await user.click(await screen.findByRole('button', { name: 'Show older listings' }));
    // Each action refetches stats; answer those with a well-formed payload.
    vi.mocked(fetch).mockImplementation(async input => ({
      ok: true,
      json: async () => (String(input) === '/api/jobs/stats'
        ? { jobs_per_day: [], submissions_per_day: [], categories: [] }
        : {}),
    }) as Response);

    await user.click(screen.getByLabelText('Dismiss Role 31'));
    expect(fetch).toHaveBeenCalledWith('/api/jobs/31', { method: 'DELETE' });
    await waitFor(() => expect(screen.queryByText('Role 31')).not.toBeInTheDocument());

    await user.click(screen.getByLabelText('Mark Role 32 as submitted'));
    expect(fetch).toHaveBeenCalledWith('/api/jobs/32/submissions', expect.objectContaining({ method: 'POST' }));
    await waitFor(() => expect(screen.queryByText('Role 32')).not.toBeInTheDocument());
    expect(screen.getByText('Role 1')).toBeInTheDocument();
  });

  it('offers the hidden older listings when no fresh ones remain', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jobs: [], stale: [rankedJob(4)] }),
    } as Response);

    render(<JobsPage />);

    await waitFor(() => {
      expect(screen.getByText('1 older listing hidden')).toBeInTheDocument();
    });
    expect(screen.queryByText('No open job listings')).not.toBeInTheDocument();
  });

  it('aborts its jobs and stats requests on unmount', () => {
    vi.mocked(fetch).mockReturnValue(new Promise(() => {}));
    const view = render(<JobsPage />);
    const signals = vi.mocked(fetch).mock.calls.map(([, init]) => (init as RequestInit).signal as AbortSignal);
    expect(signals).toHaveLength(2);
    view.unmount();
    expect(signals.every(signal => signal.aborted)).toBe(true);
  });
});
