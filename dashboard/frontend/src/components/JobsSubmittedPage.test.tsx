import { render, screen, waitFor } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsSubmittedPage } from './JobsSubmittedPage';

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

function renderWithRouter(component: React.ReactElement) {
  return render(<BrowserRouter>{component}</BrowserRouter>);
}

describe('JobsSubmittedPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('displays job submission history grouped by job', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            job: {
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
            submissions: [
              {
                id: 101,
                job_id: 1,
                submitted_at: '2026-09-23T14:30:00Z',
                resume_path: '/path/to/resume.pdf',
                cover_letter_path: null,
                notes: 'Applied via company website',
              },
            ],
          },
          {
            job: {
              id: 2,
              company: 'Tech Inc',
              title: 'Backend Engineer',
              url: 'https://example.com/job2',
              location: 'Remote',
              posted_date: '2026-09-19',
              first_seen: '2026-09-21',
              category: 'Backend',
              source: 'hn',
              deleted_at: null,
              raw: null,
            },
            submissions: [
              {
                id: 102,
                job_id: 2,
                submitted_at: '2026-09-22T10:15:00Z',
                resume_path: null,
                cover_letter_path: null,
                notes: null,
              },
            ],
          },
        ],
      }),
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    expect(screen.getByText('Frontend Developer')).toBeInTheDocument();
    expect(screen.getByText('Tech Inc')).toBeInTheDocument();
    expect(screen.getByText('Backend Engineer')).toBeInTheDocument();

    expect(screen.getAllByText('1 submission')).toHaveLength(2);
    expect(screen.getByText('Applied via company website')).toBeInTheDocument();
    expect(screen.getByText('Resume: /path/to/resume.pdf')).toBeInTheDocument();
  });

  it('displays multiple submissions for the same job', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            job: {
              id: 1,
              company: 'Repeat Corp',
              title: 'Software Engineer',
              url: 'https://example.com/job1',
              location: 'New York',
              posted_date: '2026-09-15',
              first_seen: '2026-09-16',
              category: 'Full Stack',
              source: 'manual',
              deleted_at: null,
              raw: null,
            },
            submissions: [
              {
                id: 201,
                job_id: 1,
                submitted_at: '2026-09-24T16:00:00Z',
                resume_path: '/resume_v3.pdf',
                cover_letter_path: '/cover_v2.pdf',
                notes: 'Second attempt with updated materials',
              },
              {
                id: 200,
                job_id: 1,
                submitted_at: '2026-09-18T09:00:00Z',
                resume_path: '/resume_v2.pdf',
                cover_letter_path: null,
                notes: 'First submission',
              },
            ],
          },
        ],
      }),
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Repeat Corp')).toBeInTheDocument();
    expect(screen.getByText('2 submissions')).toBeInTheDocument();

    expect(screen.getByText('Second attempt with updated materials')).toBeInTheDocument();
    expect(screen.getByText('First submission')).toBeInTheDocument();
    expect(screen.getByText('Resume: /resume_v3.pdf')).toBeInTheDocument();
    expect(screen.getByText('Cover letter: /cover_v2.pdf')).toBeInTheDocument();
  });

  it('shows dismissed badge for soft-deleted jobs', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            job: {
              id: 3,
              company: 'Dismissed Corp',
              title: 'Product Manager',
              url: 'https://example.com/job3',
              location: 'Boston',
              posted_date: '2026-09-10',
              first_seen: '2026-09-11',
              category: 'Other',
              source: 'ats_sweep',
              deleted_at: '2026-09-24T12:00:00Z',
              raw: null,
            },
            submissions: [
              {
                id: 301,
                job_id: 3,
                submitted_at: '2026-09-12T11:30:00Z',
                resume_path: null,
                cover_letter_path: null,
                notes: 'Applied before dismissal',
              },
            ],
          },
        ],
      }),
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Dismissed Corp')).toBeInTheDocument();
    expect(screen.getByText('Dismissed')).toBeInTheDocument();
    expect(screen.getByText('Applied before dismissal')).toBeInTheDocument();
  });

  it('shows empty state with link when no submissions', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({ jobs: [] }),
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('No submissions yet')).toBeInTheDocument();
    expect(screen.getByText('Browse open listings')).toBeInTheDocument();

    const link = screen.getByRole('link', { name: 'Browse open listings' });
    expect(link).toHaveAttribute('href', '/jobs');
  });

  it('shows error notification when submissions fail to load', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Failed to load submissions');
  });

  it('formats submission dates correctly', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            job: {
              id: 1,
              company: 'Date Test Corp',
              title: 'Developer',
              url: 'https://example.com/job',
              location: null,
              posted_date: null,
              first_seen: '2026-09-20',
              category: 'Other',
              source: 'manual',
              deleted_at: null,
              raw: null,
            },
            submissions: [
              {
                id: 401,
                job_id: 1,
                submitted_at: '2026-09-24T14:30:00Z',
                resume_path: null,
                cover_letter_path: null,
                notes: null,
              },
            ],
          },
        ],
      }),
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    const dateElement = screen.getByText(/Sep 24, 2026/);
    expect(dateElement).toBeInTheDocument();
  });

  it('handles submission with no notes or attachments', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            job: {
              id: 1,
              company: 'Minimal Corp',
              title: 'Engineer',
              url: 'https://example.com/job',
              location: null,
              posted_date: null,
              first_seen: '2026-09-20',
              category: 'Other',
              source: 'manual',
              deleted_at: null,
              raw: null,
            },
            submissions: [
              {
                id: 501,
                job_id: 1,
                submitted_at: '2026-09-23T10:00:00Z',
                resume_path: null,
                cover_letter_path: null,
                notes: null,
              },
            ],
          },
        ],
      }),
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Minimal Corp')).toBeInTheDocument();
    expect(screen.getByText('Engineer')).toBeInTheDocument();
    expect(screen.getByText('1 submission')).toBeInTheDocument();
  });

  it('displays all job metadata including location and category', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        jobs: [
          {
            job: {
              id: 1,
              company: 'Full Metadata Corp',
              title: 'Senior Developer',
              url: 'https://example.com/job',
              location: 'Seattle, WA',
              posted_date: '2026-09-18',
              first_seen: '2026-09-19',
              category: 'Frontend/React',
              source: 'ats_sweep',
              deleted_at: null,
              raw: null,
            },
            submissions: [
              {
                id: 601,
                job_id: 1,
                submitted_at: '2026-09-20T13:00:00Z',
                resume_path: null,
                cover_letter_path: null,
                notes: 'Test submission',
              },
            ],
          },
        ],
      }),
    } as Response);

    renderWithRouter(<JobsSubmittedPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('Full Metadata Corp')).toBeInTheDocument();
    expect(screen.getByText('Senior Developer')).toBeInTheDocument();
    expect(screen.getByText('Seattle, WA')).toBeInTheDocument();
    expect(screen.getByText('Frontend/React')).toBeInTheDocument();
  });

  it('aborts its in-flight request on unmount', () => {
    vi.mocked(fetch).mockReturnValue(new Promise(() => {}));
    const view = renderWithRouter(<JobsSubmittedPage />);
    const init = vi.mocked(fetch).mock.calls[0][1] as RequestInit;
    expect(init.signal?.aborted).toBe(false);
    view.unmount();
    expect(init.signal?.aborted).toBe(true);
  });
});
