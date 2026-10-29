import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { DashboardPage } from './DashboardPage';

// Panels that fetch on their own — not under test here.
vi.mock('./KanbanBreakdown', () => ({ KanbanBreakdown: () => null }));
vi.mock('./ShadowPricingPanel', () => ({ ShadowPricingPanel: () => null }));

const NOW = new Date().toISOString();

const PAYLOAD = {
  user: { login: 'testuser', name: 'Test', avatar_url: '', public_repos: 2, private_repos: 0 },
  repos: [
    { name: 'holoapp', url: 'https://github.com/x/holoapp', pushedAt: NOW, isPrivate: true, isArchived: false, description: null, stargazerCount: 0, defaultBranchRef: { name: 'main' } },
    { name: 'retired', url: 'https://github.com/x/retired', pushedAt: NOW, isPrivate: false, isArchived: true, description: null, stargazerCount: 0, defaultBranchRef: { name: 'main' } },
  ],
  open_pull_requests: [
    { title: 'Add thing', number: 7, url: 'https://github.com/x/holoapp/pull/7', author: { login: 'bot' }, createdAt: NOW, headRefName: 'feat/thing', baseRefName: 'main', isDraft: false, reviewDecision: '', repository: { name: 'holoapp' } },
  ],
  recent_commits: [{ repo: 'holoapp', sha: 'abc', message: 'do a thing', author: 'bot', date: NOW }],
  workflow_runs: [
    { repo: 'holoapp', name: 'pytest', status: 'completed', conclusion: 'failure', branch: 'main', created_at: NOW, url: 'https://github.com/x/holoapp/actions/1' },
    { repo: 'retired', name: 'lint', status: 'completed', conclusion: 'success', branch: 'main', created_at: NOW, url: 'https://github.com/x/retired/actions/2' },
  ],
  branches: [{ repo: 'holoapp', name: 'old/branch', protected: false }],
  fetch_errors: [],
  summary: {
    total_repos: 2,
    archived_repos: 1,
    open_prs: 1,
    draft_prs: 0,
    recent_commit_count: 1,
    repos_with_ci: 2,
    failing_ci: 1,
    fetch_errors: 0,
    repos_not_on_github: 0,
  },
  fetched_at: NOW,
  cached: false,
};

function mockApi(payload: unknown) {
  vi.stubGlobal('fetch', vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve(payload) } as Response)
  ));
}

function renderPage() {
  return render(<MemoryRouter><DashboardPage /></MemoryRouter>);
}

describe('DashboardPage — summary cards as anchors', () => {
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it('links every summary card to a section that exists on the page', async () => {
    mockApi(PAYLOAD);
    const { container } = renderPage();

    await screen.findByText('Repos');
    const cards = Array.from(container.querySelectorAll('a.summary-card'));
    expect(cards.map(card => card.getAttribute('href'))).toEqual([
      '#repos', '#open-prs', '#recent-activity', '#repos-with-ci', '#failing-ci', '#stale-branches',
    ]);

    // The point of the anchor is that it lands somewhere. A card pointing at a
    // section this page never renders is the failure mode worth catching.
    for (const card of cards) {
      const id = card.getAttribute('href')!.slice(1);
      expect(container.querySelector(`#${id}`), `no section for ${id}`).not.toBeNull();
    }
  });

  it('renders the empty sections rather than dropping their anchors', async () => {
    mockApi({
      ...PAYLOAD,
      open_pull_requests: [],
      workflow_runs: [],
      branches: [],
      summary: { ...PAYLOAD.summary, open_prs: 0, repos_with_ci: 0, failing_ci: 1 },
    });
    const { container } = renderPage();

    await screen.findByText('No open pull requests.');
    expect(screen.getByText('No stale branches.')).toBeInTheDocument();
    expect(screen.getByText('No repository reported a workflow run.')).toBeInTheDocument();
    expect(container.querySelector('#failing-ci')).not.toBeNull();
  });

  it('lists every repo the Repos count counted, archived included', async () => {
    mockApi(PAYLOAD);
    const { container } = renderPage();

    await screen.findByText('Repositories (2)');
    const section = container.querySelector('#repos') as HTMLElement;
    expect(within(section).getByRole('link', { name: 'holoapp' })).toBeInTheDocument();
    expect(within(section).getByRole('link', { name: 'retired' })).toBeInTheDocument();
    expect(within(section).getByText('archived')).toBeInTheDocument();
  });

  it('lists a row per repo with CI, failing first', async () => {
    mockApi(PAYLOAD);
    const { container } = renderPage();

    await screen.findByText('Repos with CI (2)');
    const section = container.querySelector('#repos-with-ci') as HTMLElement;
    const names = within(section).getAllByRole('link').map(link => link.textContent);
    expect(names).toEqual(['holoapp', 'retired']);
    expect(within(section).getByText(/failure · 1 run/)).toBeInTheDocument();
  });

  it('lists every failing run the card counted, past the old cap of ten', async () => {
    // The Stale Branches cap and this one were the same defect: a section
    // quietly showing fewer rows than the number that links to it.
    const runs = Array.from({ length: 12 }, (_, i) => ({
      repo: `repo-${i}`,
      name: 'pytest',
      status: 'completed',
      conclusion: 'failure',
      branch: 'main',
      created_at: NOW,
      url: `https://github.com/x/repo-${i}/actions/${i}`,
    }));
    mockApi({ ...PAYLOAD, workflow_runs: runs, summary: { ...PAYLOAD.summary, failing_ci: 12, repos_with_ci: 12 } });
    const { container } = renderPage();

    await screen.findByText('Failing CI (12)');
    const section = container.querySelector('#failing-ci') as HTMLElement;
    expect(within(section).getAllByRole('link')).toHaveLength(12);
    // The section heading and the card that links to it must agree.
    const card = container.querySelector('a.summary-card[href="#failing-ci"]');
    expect(card).toHaveAttribute('aria-label', '12 Failing CI');
  });

  it('adds the failed-fetch card only with its section', async () => {
    mockApi({ ...PAYLOAD, fetch_errors: ['repos: HTTP 502'], summary: { ...PAYLOAD.summary, fetch_errors: 1 } });
    const { container } = renderPage();

    await waitFor(() => expect(container.querySelector('#incomplete-data')).not.toBeNull());
    const card = container.querySelector('a.summary-card[href="#incomplete-data"]');
    expect(card).not.toBeNull();
  });
});
