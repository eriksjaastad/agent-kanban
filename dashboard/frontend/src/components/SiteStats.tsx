import { usePolling } from '../hooks/usePolling';
import './KanbanBreakdown.css';

type Metrics = Record<string, number | null>;

interface Source {
  status: string;
  flags: string[];
  error: string | null;
  captured_at: string | null;
  period_start: string | null;
  period_end: string | null;
  metrics: Metrics | null;
}

type Sources = Record<string, Source>;

interface Site {
  site: string;
  ran_at: string;
  exit_code: number;
  stale: boolean;
  collected_at: string | null;
  sources: Sources | null;
  baseline_at: string | null;
  baseline: Sources | null;
}

interface SiteStatsData {
  days: number;
  sites: Site[];
}

const POLL_MS = 300000;

/** One line per source: its label and the metrics to show, in order. */
const LINES: [string, string, [string, string][]][] = [
  ['gsc', 'Google search', [['impressions', 'impressions'], ['clicks', 'clicks'], ['average_position', 'avg position']]],
  ['ga4', 'Analytics', [['sessions', 'visits'], ['organic_search_sessions', 'from search']]],
  ['bing', 'Bing', [['impressions', 'impressions'], ['clicks', 'clicks'], ['in_index', 'pages indexed'], ['in_links', 'linking sites']]],
  ['ahrefs', 'Ahrefs', [['referring_domains', 'referring domains'], ['domain_rating', 'domain rating']]],
  ['pagespeed', 'PageSpeed', [['mobile_performance_worst_median', 'mobile (worst page)'], ['desktop_performance_worst_median', 'desktop (worst page)']]],
  ['screaming_frog', 'Crawl', [['html_pages', 'pages'], ['http_errors', 'errors']]],
];

function num(value: number | null | undefined): string {
  if (value === null || value === undefined) return '–';
  return Number.isInteger(value) ? String(value) : value.toFixed(value < 1 ? 2 : 1);
}

function day(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '–';
}

function line(source: Source, base: Source | undefined, metrics: [string, string][]): string {
  if (!source.metrics) return source.status === 'missing' ? 'no reading yet' : `${source.status}: ${source.error ?? 'no reading'}`;
  const values = metrics.map(([key, label]) => {
    const now = source.metrics?.[key];
    const then = base?.metrics?.[key];
    const was = then !== undefined && then !== null && num(then) !== num(now) ? ` (was ${num(then)})` : '';
    return `${num(now)} ${label}${was}`;
  });
  const period = source.period_end ? ` · to ${source.period_end}` : source.captured_at ? ` · from ${day(source.captured_at)}` : '';
  const stale = source.flags.includes('stale') ? ' · stale' : '';
  return values.join(', ') + period + stale;
}

/** Each site's latest search, analytics, Bing, Ahrefs, PageSpeed and crawl numbers, with the change over 30 days (#8868). */
export function SiteStats() {
  const { data, error } = usePolling<SiteStatsData>(async (signal) => {
    const response = await fetch('/api/stats/sites', { signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }, POLL_MS, [], { timeoutMs: 10000 });

  return (
    <section className="dashboard-section" aria-label="Website stats">
      <h2>Website stats</h2>
      {error && <p className="breakdown-error" role="alert">Website stats unavailable: {error.message}</p>}
      {data && data.sites.length === 0 && <p className="breakdown-empty">No collector runs yet (pt site-stats run).</p>}
      {data?.sites.map(site => (
        <div key={site.site}>
          <h3>{site.site}</h3>
          {site.stale && (
            <p className="breakdown-error" role="alert">No run since {day(site.ran_at)}: the daily collector may have stopped</p>
          )}
          {site.exit_code !== 0 && site.exit_code !== 2 && (
            <p className="breakdown-error" role="alert">Last run {site.ran_at} failed (exit {site.exit_code})</p>
          )}
          {site.sources === null ? (
            <p className="breakdown-empty">No numbers collected yet.</p>
          ) : (
            <>
              <p className="breakdown-empty">
                Collected {day(site.collected_at)}{site.baseline_at && site.baseline_at !== site.collected_at ? `; "was" is ${day(site.baseline_at)}` : ''}.
              </p>
              <ul className="breakdown-list">
                {LINES.filter(([key]) => site.sources?.[key]).map(([key, label, metrics]) => (
                  <li key={key} className="breakdown-row">
                    <span className="breakdown-project">{label}</span>
                    <span className="breakdown-count">{line(site.sources![key], site.baseline?.[key], metrics)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      ))}
    </section>
  );
}
