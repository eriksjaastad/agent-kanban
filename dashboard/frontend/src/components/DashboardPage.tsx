import { useEffect, useState, useCallback } from 'react';
import { PageShell } from './PageShell';
import { KanbanBreakdown } from './KanbanBreakdown';
import { ShadowPricingPanel } from './ShadowPricingPanel';
import { isWithinRecentWindow, timeAgo } from '../utils/recentActivity';
import './DashboardPage.css';

interface GitHubData {
  user?: {
    login: string;
    name: string;
    avatar_url: string;
    public_repos: number;
    private_repos: number;
  };
  repos?: Array<{
    name: string;
    url: string;
    pushedAt: string;
    isPrivate: boolean;
    isArchived: boolean;
    description: string | null;
    stargazerCount: number;
    defaultBranchRef: { name: string } | null;
  }>;
  open_pull_requests?: Array<{
    title: string;
    number: number;
    url: string;
    author: { login: string };
    createdAt: string;
    headRefName: string;
    baseRefName: string;
    isDraft: boolean;
    reviewDecision: string;
    repository: { name: string };
    statusCheckRollup?: Array<{ state: string }>;
  }>;
  recent_commits?: Array<{
    repo: string;
    sha: string;
    message: string;
    author: string;
    date: string;
  }>;
  workflow_runs?: Array<{
    repo: string;
    name: string;
    status: string;
    conclusion: string | null;
    branch: string;
    created_at: string;
    url: string;
  }>;
  branches?: Array<{
    repo: string;
    name: string;
    protected: boolean;
  }>;
  fetch_errors?: string[];
  summary?: {
    total_repos: number;
    archived_repos: number;
    open_prs: number;
    draft_prs: number;
    recent_commit_count: number;
    repos_with_ci: number;
    failing_ci: number;
    fetch_errors: number;
    repos_not_on_github: number;
  };
  fetched_at?: string;
  cached?: boolean;
  refreshing?: boolean;
  stale?: boolean;
  refresh_error?: string;
  retry_after_seconds?: number;
  error?: string;
}

const ACTIVITY_LIMIT = 10;

// Each summary number is a link to the section that lists what it counted, so
// "3 failing CI" is one click from knowing which three. Every anchor here must
// have a section rendered below — including when the count is zero, or the
// link lands nowhere.
function SummaryCard({ anchor, value, label, bad }: {
  anchor: string;
  value: number | undefined;
  label: string;
  bad?: boolean;
}) {
  return (
    <a
      className={`summary-card${bad ? ' highlight-bad' : ''}`}
      href={`#${anchor}`}
      aria-label={`${value ?? 0} ${label}`}
    >
      <span className="summary-number">{value}</span>
      <span className="summary-label">{label}</span>
    </a>
  );
}

function GitHubClock({ fetching }: { fetching: boolean }) {
  if (!fetching) return null;
  return <span className="github-fetching"><span aria-hidden="true">◷</span> Fetching GitHub…</span>;
}

function RecentActivity({ repos, commitsByRepo, commitCount, fetching }: {
  fetching: boolean;
  repos: GitHubData['repos'] & Array<{ pushedAt: string }>;
  commitsByRepo: Record<string, GitHubData['recent_commits']>;
  commitCount: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const toggle = useCallback(() => setExpanded(e => !e), []);
  const visible = expanded ? repos : repos.slice(0, ACTIVITY_LIMIT);
  const hasMore = repos.length > ACTIVITY_LIMIT;

  return (
    <section className="dashboard-section" id="recent-activity">
      <h2>Recent Activity — {commitCount} commits (7 days) <GitHubClock fetching={fetching} /></h2>
      <div className="activity-list">
        {visible.map((repo, i) => (
          <div key={i} className="activity-item">
            <span className="activity-repo">{repo.name}</span>
            <span className="activity-meta">{timeAgo(repo.pushedAt)}</span>
            <span className="activity-commits">
              {commitsByRepo[repo.name]?.length || 0} commits
            </span>
          </div>
        ))}
      </div>
      {repos.length === 0 && <p className="section-empty">No repository activity in the last 7 days.</p>}
      {hasMore && (
        <button className="expand-button" onClick={toggle}>
          {expanded ? 'Show less' : `Show all ${repos.length} repos`}
        </button>
      )}
    </section>
  );
}

// A full GitHub collection takes about two minutes. Polling it every 2s made
// ~50 calls per refresh, most of /api/github's traffic (#8093), so back off.
const REFRESH_POLL_MS = [5000, 10000, 20000, 30000];
const IDLE_POLL_MS = 60000;

export function DashboardPage() {
  const [data, setData] = useState<GitHubData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [refreshing, setRefreshing] = useState(true);

  useEffect(() => {
    let disposed = false;
    let pollTimer: ReturnType<typeof setTimeout>;
    let controller: AbortController;
    let refreshPolls = 0;
    let parked = false;

    async function load() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      let delay = IDLE_POLL_MS;
      try {
        const response = await fetch('/api/github', { signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const next: GitHubData = await response.json();
        if (disposed) return;
        if (next.summary) setData(next);
        setError(next.refresh_error || next.error || null);
        setRefreshing(Boolean(next.refreshing));
        if (next.refreshing) {
          delay = REFRESH_POLL_MS[Math.min(refreshPolls, REFRESH_POLL_MS.length - 1)];
          refreshPolls += 1;
        } else {
          refreshPolls = 0;
          if (next.retry_after_seconds) {
            delay = Math.min(IDLE_POLL_MS, Math.max(2000, next.retry_after_seconds * 1000));
          }
        }
      } catch {
        if (disposed) return;
        refreshPolls = 0;
        setError('GitHub is unavailable. Retrying shortly.');
        setRefreshing(false);
      } finally {
        clearTimeout(timeout);
        if (!disposed) {
          setLoading(false);
          pollTimer = setTimeout(poll, delay);
        }
      }
    }

    // A hidden tab does not poll, from the first load on: past the cache's five
    // minutes every poll starts a GitHub collection nobody is looking at.
    // Showing the tab loads at once.
    function poll() {
      if (document.hidden) {
        parked = true;
        return;
      }
      void load();
    }

    function onVisibilityChange() {
      if (disposed || document.hidden || !parked) return;
      parked = false;
      void load();
    }

    document.addEventListener('visibilitychange', onVisibilityChange);
    poll();
    return () => {
      disposed = true;
      clearTimeout(pollTimer);
      controller?.abort();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, []);

  const fetching = loading || refreshing;
  const { summary, repos, open_pull_requests, recent_commits, workflow_runs, branches, fetch_errors } = data || {};
  const fetchErrors = fetch_errors || [];

  // Group commits by repo
  const commitsByRepo: Record<string, typeof recent_commits> = {};
  for (const c of recent_commits || []) {
    if (!commitsByRepo[c.repo]) commitsByRepo[c.repo] = [];
    commitsByRepo[c.repo]!.push(c);
  }

  // Every repo the "Repos" count counted, newest push first. Archived repos are
  // included because summary.total_repos includes them — a section that
  // filtered them would disagree with the number that links to it.
  const allRepos = [...(repos || [])].sort(
    (a, b) => new Date(b.pushedAt || 0).getTime() - new Date(a.pushedAt || 0).getTime()
  );
  const openPrs = open_pull_requests || [];

  // Active repos (pushed in last 7 days, not archived). The previous filter
  // dropped only `isArchived`, so Recent Activity listed month-old pushes and
  // the ages looked uniformly wrong next to a "7 days" heading (#7180).
  const activeRepos = (repos || [])
    .filter(r => !r.isArchived && r.pushedAt && isWithinRecentWindow(r.pushedAt))
    .sort((a, b) => new Date(b.pushedAt).getTime() - new Date(a.pushedAt).getTime());

  // Failing CI — only show failures on main/master or branches with open PRs
  // (filters out stale failures on merged/closed branches)
  const openPrBranches = new Set(
    (open_pull_requests || []).map(pr => `${pr.repository?.name}:${pr.headRefName}`)
  );
  // Same predicate the backend uses for summary.failing_ci, and deliberately
  // unsliced: the card links here claiming a number, so a cap would hide rows
  // the number counted.
  const failingRuns = (workflow_runs || [])
    .filter(r => {
      if (r.conclusion !== 'failure') return false;
      const isDefaultBranch = r.branch === 'main' || r.branch === 'master';
      const hasOpenPr = openPrBranches.has(`${r.repo}:${r.branch}`);
      return isDefaultBranch || hasOpenPr;
    });

  // Stale branches (non-main, non-protected)
  const staleBranches = (branches || [])
    .filter(b => !b.protected && b.name !== 'main' && b.name !== 'master');

  // Repos that reported a workflow run, which is what summary.repos_with_ci
  // counts, each with its most recent run.
  const runsByRepo: Record<string, NonNullable<GitHubData['workflow_runs']>> = {};
  for (const run of workflow_runs || []) {
    if (!runsByRepo[run.repo]) runsByRepo[run.repo] = [];
    runsByRepo[run.repo].push(run);
  }
  const ciRepos = Object.entries(runsByRepo)
    .map(([repo, runs]) => ({
      repo,
      runs: runs.length,
      latest: [...runs].sort(
        (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      )[0],
    }))
    .sort((a, b) => {
      const aFailed = a.latest?.conclusion === 'failure';
      const bFailed = b.latest?.conclusion === 'failure';
      if (aFailed !== bFailed) return aFailed ? -1 : 1;
      return a.repo.localeCompare(b.repo);
    });

  // Group stale branches by repo
  const branchesByRepo: Record<string, string[]> = {};
  for (const b of staleBranches) {
    if (!branchesByRepo[b.repo]) branchesByRepo[b.repo] = [];
    branchesByRepo[b.repo].push(b.name);
  }

  return (
    <PageShell
      title="Dashboard"
      subtitle={summary ? `${summary.total_repos} repos · ${summary.recent_commit_count} commits this week${fetchErrors.length ? ` · ${fetchErrors.length} incomplete` : ''}` : 'Project activity and API usage'}
    >
      <div className="dashboard-grid">
        {/* Kanban board breakdown — the work in flight, before anything else */}
        <KanbanBreakdown />

        {/* Shadow Pricing — subscription value */}
        <ShadowPricingPanel />

        <div className="github-status" role="status">
          <GitHubClock fetching={fetching} />
          {data?.fetched_at && (
            <span>GitHub updated {new Date(data.fetched_at).toLocaleString()}{data.stale ? ' · showing previous results' : ''}</span>
          )}
        </div>
        {error && <div className="dashboard-error" role="alert">{error}{data ? ' Showing the last available results.' : ''}</div>}
        {!data && (
          <>
            {['GitHub Summary', 'Repositories', 'Open Pull Requests', 'Recent Activity', 'Repos with CI', 'Failing CI', 'Stale Branches'].map(title => (
              <section className="dashboard-section" key={title}>
                <h2>{title} <GitHubClock fetching={fetching} /></h2>
                <p>{fetching ? 'Waiting for GitHub information…' : 'GitHub information is unavailable.'}</p>
              </section>
            ))}
          </>
        )}
        {data && <>
        {/* Summary Cards */}
        <div className="dashboard-summary" aria-label="GitHub summary">
          <GitHubClock fetching={fetching} />
          <SummaryCard anchor="repos" value={summary?.total_repos} label="Repos" />
          <SummaryCard anchor="open-prs" value={summary?.open_prs} label="Open PRs" />
          <SummaryCard anchor="recent-activity" value={summary?.recent_commit_count} label="Commits (7d)" />
          <SummaryCard anchor="repos-with-ci" value={summary?.repos_with_ci} label="Repos w/ CI" />
          <SummaryCard anchor="failing-ci" value={summary?.failing_ci} label="Failing CI" bad />
          <SummaryCard
            anchor="stale-branches"
            value={Object.keys(branchesByRepo).length}
            label="Repos w/ stale branches"
          />
          {fetchErrors.length > 0 && (
            <SummaryCard anchor="incomplete-data" value={fetchErrors.length} label="Failed fetches" bad />
          )}

        </div>

        {/* Incomplete data — every number above is a floor, not a total, while
            this is non-empty. Without it the dashboard reports partial results
            as if they were complete, which is the same silent degradation as
            the bug that made this list necessary (#6749). */}
        {fetchErrors.length > 0 && (
          <section className="dashboard-section" id="incomplete-data" role="alert">
            <h2>Incomplete data</h2>
            <p className="fetch-errors-note">
              GitHub data could not be retrieved for the following. Counts above exclude them,
              so treat every figure as a minimum.
            </p>
            <ul className="fetch-errors-list">
              {fetchErrors.map((err, i) => (
                <li key={i}>{err}</li>
              ))}
            </ul>
          </section>
        )}

        {/* Repos — what the "Repos" count is counting */}
        <section className="dashboard-section" id="repos">
          <h2>Repositories ({allRepos.length}) <GitHubClock fetching={fetching} /></h2>
          <div className="repo-list">
            {allRepos.map(repo => (
              <div key={repo.name} className="repo-item">
                <a href={repo.url} target="_blank" rel="noopener noreferrer">{repo.name}</a>
                {repo.isPrivate && <span className="repo-badge">private</span>}
                {repo.isArchived && <span className="repo-badge archived">archived</span>}
                <span className="repo-meta">
                  {repo.pushedAt ? `pushed ${timeAgo(repo.pushedAt)}` : 'never pushed'}
                </span>
              </div>
            ))}
          </div>
          {allRepos.length === 0 && <p className="section-empty">No repositories returned.</p>}
        </section>

        {/* Open PRs */}
        <section className="dashboard-section" id="open-prs">
          <h2>Open Pull Requests ({openPrs.length}) <GitHubClock fetching={fetching} /></h2>
          <div className="pr-list">
            {openPrs.map((pr, i) => (
              <div key={i} className="pr-item">
                <a href={pr.url} target="_blank" rel="noopener noreferrer">
                  {pr.repository?.name}#{pr.number}
                </a>
                <span className="pr-title">{pr.title}</span>
                <span className={`pr-badge ${pr.isDraft ? 'draft' : pr.reviewDecision?.toLowerCase() || 'pending'}`}>
                  {pr.isDraft ? 'Draft' : pr.reviewDecision || 'Pending'}
                </span>
                <span className="pr-meta">{timeAgo(pr.createdAt)}</span>
              </div>
            ))}
          </div>
          {openPrs.length === 0 && <p className="section-empty">No open pull requests.</p>}
        </section>

        {/* Recent Activity */}
        <RecentActivity
          repos={activeRepos}
          commitsByRepo={commitsByRepo}
          commitCount={summary?.recent_commit_count ?? 0}
          fetching={fetching}
        />

        {/* Repos with CI — the repos that reported workflow runs */}
        <section className="dashboard-section" id="repos-with-ci">
          <h2>Repos with CI ({ciRepos.length}) <GitHubClock fetching={fetching} /></h2>
          <div className="ci-list">
            {ciRepos.map(({ repo, runs, latest }) => (
              <div key={repo} className={`ci-item${latest?.conclusion === 'failure' ? ' failing' : ''}`}>
                {latest ? (
                  <a href={latest.url} target="_blank" rel="noopener noreferrer">{repo}</a>
                ) : (
                  <span>{repo}</span>
                )}
                <span className="ci-name">{latest?.name}</span>
                <span className="ci-branch">{latest?.branch}</span>
                <span className="ci-meta">
                  {latest?.conclusion || latest?.status || 'unknown'} · {runs} run{runs === 1 ? '' : 's'}
                </span>
              </div>
            ))}
          </div>
          {ciRepos.length === 0 && <p className="section-empty">No repository reported a workflow run.</p>}
        </section>

        {/* Failing CI */}
        <section className="dashboard-section" id="failing-ci">
          <h2>Failing CI ({failingRuns.length}) <GitHubClock fetching={fetching} /></h2>
          <div className="ci-list">
            {failingRuns.map((run, i) => (
              <div key={i} className="ci-item failing">
                <a href={run.url} target="_blank" rel="noopener noreferrer">
                  {run.repo}
                </a>
                <span className="ci-name">{run.name}</span>
                <span className="ci-branch">{run.branch}</span>
                <span className="ci-meta">{timeAgo(run.created_at)}</span>
              </div>
            ))}
          </div>
          {failingRuns.length === 0 && <p className="section-empty">No failing runs on a default branch or an open PR.</p>}
        </section>

        {/* Stale Branches — every repo the count above claims, not a top slice */}
        <section className="dashboard-section" id="stale-branches">
          <h2>Stale Branches ({staleBranches.length} across {Object.keys(branchesByRepo).length} repos) <GitHubClock fetching={fetching} /></h2>
          <div className="branch-list">
            {Object.entries(branchesByRepo)
              .sort((a, b) => b[1].length - a[1].length)
              .map(([repo, branchNames]) => (
                <div key={repo} className="branch-item">
                  <span className="branch-repo">{repo}</span>
                  <span className="branch-names">{branchNames.join(', ')}</span>
                </div>
              ))}
          </div>
          {staleBranches.length === 0 && <p className="section-empty">No stale branches.</p>}
        </section>

        {/* Raw data dump for exploration */}
        <details className="dashboard-section raw-data">
          <summary>Raw API Response ({JSON.stringify(data).length} bytes)</summary>
          <pre>{JSON.stringify(data, null, 2)}</pre>
        </details>
        </>}
      </div>
    </PageShell>
  );
}
