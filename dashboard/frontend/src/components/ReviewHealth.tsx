import { usePolling } from '../hooks/usePolling';
import './KanbanBreakdown.css';

interface Week {
  week_start: string;
  merged: number;
  reviewed: number;
  unpinned: number;
  median_rounds: number | null;
  with_fail: number;
  overthinking: number;
}

interface ReviewHealthData {
  weeks?: Week[];
  refresh_error: string | null;
}

const POLL_MS = 60000;

/** Per week, merged PRs with a recorded review, median review rounds, FAIL rounds, overthinking calls (#8848). */
export function ReviewHealth() {
  const { data, error } = usePolling<ReviewHealthData>(async (signal) => {
    const response = await fetch('/api/stats/review-health', { signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }, POLL_MS, [], { timeoutMs: 10000 });

  const failure = error?.message ?? data?.refresh_error;
  return (
    <section className="dashboard-section" aria-label="Review health">
      <h2>Review health</h2>
      {failure && <p className="breakdown-error" role="alert">Review health unavailable: {failure}</p>}
      {data && !data.weeks && !failure && <p className="breakdown-empty">Reading merged PRs…</p>}
      {data?.weeks && (
        <>
          <p className="breakdown-empty">
            From each merged PR's review record; unpinned means a PASS naming no commit. Findings per PR are not recorded, only each round's verdict.
          </p>
          <ul className="breakdown-list">
            {data.weeks.map(week => (
              <li key={week.week_start} className="breakdown-row">
                <span className="breakdown-project">{week.week_start}</span>
                <span className="breakdown-count">
                  {week.reviewed} of {week.merged} reviewed
                  {week.unpinned > 0 && ` (+${week.unpinned} unpinned)`}
                  {week.median_rounds !== null && ` · median ${week.median_rounds} round${week.median_rounds === 1 ? '' : 's'}`}
                  {` · ${week.with_fail} with a FAIL · ${week.overthinking} called overthinking`}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
