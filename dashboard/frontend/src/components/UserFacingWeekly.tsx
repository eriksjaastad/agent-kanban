import { usePolling } from '../hooks/usePolling';
import './KanbanBreakdown.css';

interface Week {
  week_start: string;
  user_facing: number;
  internal: number;
}

interface MergedPrs {
  weeks?: Week[];
  not_on_github?: string[];
  refresh_error: string | null;
}

interface WeeklySplit {
  user_facing_projects: string[];
  weeks: Week[];
  merged_prs: MergedPrs;
}

// Merged PRs are counted in the background, so a minute brings the first count in soon.
const POLL_MS = 60000;

function split(week: Week | undefined, unit: string) {
  if (!week) return '';
  const total = week.user_facing + week.internal;
  const share = total ? Math.round((100 * week.user_facing) / total) : 0;
  return `${week.user_facing} of ${total} ${unit} · ${share}%`;
}

/** Done cards and merged PRs per week that landed on a user-facing project vs internal tooling (#8844). */
export function UserFacingWeekly() {
  // A failed fetch keeps the last good weeks on screen and names the failure.
  const { data, error } = usePolling<WeeklySplit>(async (signal) => {
    const response = await fetch('/api/stats/user-facing-weekly', { signal });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${response.status}`);
    }
    return response.json();
  }, POLL_MS, [], { timeoutMs: 10000 });

  return (
    <section className="dashboard-section" aria-label="Reached users">
      <h2>Reached users</h2>
      {error && <p className="breakdown-error" role="alert">Weekly split unavailable: {error.message}</p>}
      {data && (
        <>
          <p className="breakdown-empty">
            {data.user_facing_projects.length
              ? `User-facing: ${data.user_facing_projects.join(', ')}`
              : 'No user-facing projects listed yet.'}
            {' '}Edit [dashboard] user_facing in the pt config.
          </p>
          {data.merged_prs.refresh_error && <p className="breakdown-error" role="alert">{data.merged_prs.refresh_error}</p>}
          {!data.merged_prs.weeks && !data.merged_prs.refresh_error && <p className="breakdown-empty">Counting merged PRs…</p>}
          {!!data.merged_prs.not_on_github?.length && (
            <p className="breakdown-error">Not on GitHub: {data.merged_prs.not_on_github.join(', ')}</p>
          )}
          <ul className="breakdown-list">
            {data.weeks.map(week => {
              const prs = data.merged_prs.weeks?.find(w => w.week_start === week.week_start);
              return (
                <li key={week.week_start} className="breakdown-row">
                  <span className="breakdown-project">Week of {week.week_start}</span>
                  <span className="breakdown-count">{split(week, 'cards')}</span>
                  {prs && <span className="breakdown-count">{split(prs, 'PRs')}</span>}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
