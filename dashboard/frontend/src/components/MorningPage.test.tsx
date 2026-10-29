import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MorningPage } from './MorningPage';

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

function morningPayload(overrides: Record<string, unknown> = {}) {
  return {
    date: '2026-09-26',
    source: '/tmp/plans/README.md',
    intro: [[{ type: 'text', text: 'A calm morning plan.' }]],
    steps: [
      {
        number: '0',
        channel: 'Replies',
        time: '2 min',
        description: [
          { type: 'text', text: 'Check ' },
          { type: 'link', text: 'replies', href: 'https://example.com/replies' },
        ],
      },
      {
        number: '1',
        channel: 'Hacker News',
        time: '2 min',
        description: [{ type: 'code', text: 'hn-jobs-*.txt' }],
      },
    ],
    next_morning: [[{ type: 'strong', text: 'Run the table above.' }]],
    snapshots: [
      {
        kind: 'freelance',
        filename: 'hn-jobs-2026-09-26-freelance.txt',
        present: false,
        text: null,
      },
      {
        kind: 'hiring',
        filename: 'hn-jobs-2026-09-26-hiring.txt',
        present: true,
        text: 'Who is hiring?',
      },
    ],
    ...overrides,
  };
}

function okResponse(payload: unknown): Response {
  return {
    ok: true,
    json: async () => payload,
  } as Response;
}

function errorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ detail }),
  } as Response;
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
}

function mockFetchByUrl(routes: Record<string, Response>) {
  vi.mocked(fetch).mockImplementation(async (input) => {
    const url = requestUrl(input);
    const route = routes[url];
    if (route) return route;
    throw new Error(`Unexpected fetch: ${url}`);
  });
}

function mockMorningAndEmptyContacts() {
  mockFetchByUrl({
    '/api/morning': okResponse(morningPayload()),
    '/api/outreach/contacts': okResponse({ contacts: [] }),
  });
}

describe('MorningPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.localStorage.clear();
  });

  it('renders the checklist, intro, today section, and HN snapshots', async () => {
    mockMorningAndEmptyContacts();

    render(<MorningPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByText('A calm morning plan.')).toBeInTheDocument();
    expect(screen.getByText('Replies')).toBeInTheDocument();
    expect(screen.getByText('Hacker News')).toBeInTheDocument();
    expect(screen.getByText('Today')).toBeInTheDocument();
    expect(screen.getByText('Run the table above.')).toBeInTheDocument();
    expect(screen.getByText('hn-jobs-2026-09-26-freelance.txt')).toBeInTheDocument();
    expect(screen.getByText('Not found — check hn-jobs-cron.log')).toBeInTheDocument();
    expect(screen.getByText('Who is hiring?')).toBeInTheDocument();

    const link = screen.getByRole('link', { name: 'replies' });
    expect(link).toHaveAttribute('href', 'https://example.com/replies');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('persists checked steps to localStorage for the API date and survives re-render', async () => {
    mockMorningAndEmptyContacts();

    const { rerender } = render(<MorningPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    const repliesCheckbox = screen.getByLabelText(/Replies/) as HTMLInputElement;
    expect(repliesCheckbox.checked).toBe(false);

    fireEvent.click(repliesCheckbox);

    expect(repliesCheckbox.checked).toBe(true);
    expect(window.localStorage.getItem('morning-checklist:2026-09-26')).toBe(
      JSON.stringify(['0'])
    );

    rerender(<MorningPage />);

    expect((screen.getByLabelText(/Replies/) as HTMLInputElement).checked).toBe(true);
  });

  it('does not pre-check from a stored key for a different date', async () => {
    window.localStorage.setItem('morning-checklist:2026-09-25', JSON.stringify(['0']));
    mockMorningAndEmptyContacts();

    render(<MorningPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect((screen.getByLabelText(/Replies/) as HTMLInputElement).checked).toBe(false);
    expect(window.localStorage.getItem('morning-checklist:2026-09-26')).toBeNull();
  });

  it('shows the API error detail when the fetch fails', async () => {
    mockFetchByUrl({
      '/api/morning': errorResponse(
        503,
        'Morning plan source not found: /tmp/plans/README.md'
      ),
      '/api/outreach/contacts': okResponse({ contacts: [] }),
    });

    render(<MorningPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Morning plan source not found: /tmp/plans/README.md'
    );
    expect(screen.queryByText('Replies')).not.toBeInTheDocument();
    expect(screen.queryByText('No morning plan to show right now.')).not.toBeInTheDocument();
  });

  it('still renders the outreach panel when the morning plan fails', async () => {
    mockFetchByUrl({
      '/api/morning': errorResponse(503, 'Morning plan source not found'),
      '/api/outreach/contacts': okResponse({ contacts: [] }),
    });

    render(<MorningPage />);

    await waitFor(() => {
      expect(screen.queryByText('Loading...')).not.toBeInTheDocument();
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Morning plan source not found');
    expect(screen.getByRole('button', { name: /People to contact/ })).toBeInTheDocument();
    expect(screen.getByLabelText('Name or email')).toBeInTheDocument();
  });

  it('aborts its in-flight request on unmount', () => {
    vi.mocked(fetch).mockReturnValue(new Promise(() => {}));
    const view = render(<MorningPage />);
    const init = vi.mocked(fetch).mock.calls[0][1] as RequestInit;
    expect(init.signal?.aborted).toBe(false);
    view.unmount();
    expect(init.signal?.aborted).toBe(true);
  });
});
