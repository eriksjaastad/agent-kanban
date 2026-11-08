import { usePolling } from '../hooks/usePolling';
import './KanbanBreakdown.css';

interface Site {
  project: string;
  url: string;
  changes: number[] | null;
}

interface LiveSites {
  week_starts?: string[];
  sites?: Site[];
  not_live?: { project: string; status: number | string }[];
  refresh_error: string | null;
}

const POLL_MS = 60000;

/** Per live site, user-visible (feat/fix) changes in each of the last 8 weeks, zeros shown (#8845). */
export function LiveSiteChanges() {
  const { data, error } = usePolling<LiveSites>(async (signal) => {
    const response = await fetch('/api/stats/live-site-changes', { signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }, POLL_MS, [], { timeoutMs: 10000 });

  const failure = error?.message ?? data?.refresh_error;
  return (
    <section className="dashboard-section" aria-label="Shipped to users">
      <h2>Shipped to users</h2>
      {failure && <p className="breakdown-error" role="alert">Live sites unavailable: {failure}</p>}
      {data && !data.sites && !failure && <p className="breakdown-empty">Checking live sites…</p>}
      {data?.sites && data.week_starts && (
        <>
          <p className="breakdown-empty">
            feat/fix commits on main per week, from {data.week_starts[0]}; a live site answers 200 now.
          </p>
          <ul className="breakdown-list">
            {data.sites.map(site => (
              <li key={site.project} className="breakdown-row">
                <a className="breakdown-project" href={site.url}>{site.project}</a>
                <span className="breakdown-count">
                  {site.changes ? site.changes.join(' · ') : 'no checkout on this Mac'}
                </span>
              </li>
            ))}
          </ul>
          {!!data.not_live?.length && (
            <p className="breakdown-empty">
              Not live now: {data.not_live.map(s => `${s.project} (${s.status})`).join(', ')}
            </p>
          )}
        </>
      )}
    </section>
  );
}
