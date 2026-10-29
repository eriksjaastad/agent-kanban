import { fetchScheduledJobs } from '../api';
import type { ScheduledJob } from '../api';
import { useRequest } from '../hooks/useRequest';

// What the job's last run tells you, in words. launchctl reports an exit code
// only for a loaded job; a negative one is the signal that killed it.
function lastRun(job: ScheduledJob): string {
  if (job.running) return 'Running now';
  if (job.last_exit === null) return job.status === 'active' ? 'Not run yet' : '—';
  if (job.last_exit === 0) return 'OK';
  return job.last_exit < 0 ? `Killed (signal ${-job.last_exit})` : `Failed (exit ${job.last_exit})`;
}

const NO_PROJECT = 'No project';
const anchor = (project: string) => `sched-${project || 'none'}`;

// Live machine state from /api/scheduled-jobs (#8377). It loads on its own, not
// with the calendar's all-or-nothing request: reading launchctl and crontab can
// take seconds, and a failure here must not hide the calendar. `projects` is
// the calendar's project list, used only to name projects with no jobs.
export function ScheduledJobsPanel({ projects = [] }: { projects?: string[] }) {
  const request = useRequest(signal => fetchScheduledJobs(signal), []);
  const jobs = request.data?.jobs ?? [];
  const byProject = new Map<string, ScheduledJob[]>();
  for (const job of jobs) byProject.set(job.project, [...(byProject.get(job.project) ?? []), job]);
  // Projects A-Z, then the jobs no project owns.
  const groups = [...byProject.keys()].sort((a, b) => (!a ? 1 : !b ? -1 : a.localeCompare(b)));
  const without = projects.filter(p => !byProject.has(p)).sort((a, b) => a.localeCompare(b));
  // A source or a job that could not be read may belong to any project, so no project can be called empty.
  const complete = !!request.data && request.data.errors.length === 0 && jobs.every(j => !j.error);

  return (
    <section className="cal-sched" aria-labelledby="cal-sched-title">
      <h3 id="cal-sched-title" className="cal-sched-title">On this Mac now</h3>
      <p className="cal-crons-intro">
        Every launchd job in ~/Library/LaunchAgents, job plists kept in project <code>launchd/</code> folders,
        and this user's crontab. Crontab jobs show <em>in crontab</em>: cron keeps no record of runs.
      </p>
      {request.loading && !request.data && <div className="cal-crons-empty">Reading this Mac's scheduled jobs…</div>}
      {!request.loading && request.error && (
        <div className="cal-error" role="alert">Could not load scheduled jobs: {request.error.message}</div>
      )}
      {request.data?.errors.map(e => (
        <div key={e} className="cal-error" role="alert">Could not read: {e}</div>
      ))}
      {request.data && jobs.length === 0 && <div className="cal-crons-empty">No scheduled jobs found.</div>}
      {groups.length > 0 && (
        <nav className="cal-sched-chips" aria-label="Projects with scheduled jobs">
          {groups.map(p => (
            <a key={p} href={`#${anchor(p)}`} className="cal-cron-project">
              {p || NO_PROJECT} ({byProject.get(p)!.length})
            </a>
          ))}
        </nav>
      )}
      {complete && without.length > 0 && (
        <p className="cal-crons-intro">No scheduled jobs: {without.join(', ')}</p>
      )}
      {request.data && !complete && projects.length > 0 && (
        <p className="cal-crons-intro">Some jobs could not be read, so this can't list the projects with none.</p>
      )}
      {groups.map(p => (
        <div key={p} id={anchor(p)} className="cal-sched-scroll">
          <h4 className="cal-sched-project">{p || NO_PROJECT}</h4>
          <table className="cal-cron-table" aria-label={`Scheduled jobs: ${p || NO_PROJECT}`}>
            <thead>
              <tr>
                <th>Job</th>
                <th>Status</th>
                <th>Schedule</th>
                <th>Last run</th>
              </tr>
            </thead>
            <tbody>
              {byProject.get(p)!.map((job, i) => (
                // A crontab can hold the same line twice, so no field is unique; the list is never reordered.
                <tr key={i}>
                  <td>
                    <code className="cal-cron-cmd cal-sched-settings">{job.label}</code>
                    {job.error && <div className="cal-error">{job.error}</div>}
                  </td>
                  <td>
                    <span className={`cal-status-chip cal-status-chip--${job.status.replace(/\s+/g, '-')}`}>
                      {job.status}
                    </span>
                  </td>
                  <td className="cal-sched-settings">
                    {job.schedule || '—'}
                    {job.settings && (
                      <details>
                        <summary>Full settings</summary>
                        <code className="cal-cron-cmd" title={job.path}>{job.settings}</code>
                      </details>
                    )}
                  </td>
                  <td className="cal-cron-last">{lastRun(job)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
