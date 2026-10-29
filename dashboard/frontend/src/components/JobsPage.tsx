import { useEffect, useMemo, useState } from 'react';
import { PageShell } from './PageShell';
import { Spinner } from './Spinner';
import { Notification } from './Notification';
import { JobStatsChart } from './JobStatsChart';
import { CategoryTable } from './CategoryTable';
import { RankedListControls, TOP_LISTINGS } from './RankedListControls';
import { useRequest } from '../hooks/useRequest';
import type { Job } from '../types';
import './JobsPage.css';

// Both lists arrive ranked best-first from the API; `stale` is older than 30 days.
interface RankedJobs {
  fresh: Job[];
  stale: Job[];
}

interface JobStats {
  jobs_per_day: Array<{ date: string; count: number }>;
  submissions_per_day: Array<{ date: string; count: number }>;
  categories: Array<{ category: string; count: number }>;
}

export function JobsPage() {
  const [notification, setNotification] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
  const [queuingPrompts, setQueuingPrompts] = useState<Set<number>>(new Set());
  const [submittingJobs, setSubmittingJobs] = useState<Set<number>>(new Set());
  // Jobs dismissed or submitted on this page; hidden from the loaded list.
  const [removedJobs, setRemovedJobs] = useState<Set<number>>(new Set());

  const [showStale, setShowStale] = useState(false);
  const [showAll, setShowAll] = useState(false);

  const { data: loadedJobs, error: jobsError, loading } = useRequest<RankedJobs>(async (signal) => {
    const response = await fetch('/api/jobs', { signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const data = await response.json();
    return { fresh: data.jobs || [], stale: data.stale || [] };
  }, []);
  // reload() aborts an in-flight stats request, so an older refresh that
  // resolves late can never clobber a newer one.
  const { data: stats, error: statsFailure, reload: loadStats } = useRequest<JobStats>(async (signal) => {
    const response = await fetch('/api/jobs/stats', { signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.json();
  }, []);
  const statsError = statsFailure !== null;
  const jobs = useMemo(
    () => (loadedJobs?.fresh ?? []).filter(job => !removedJobs.has(job.id)),
    [loadedJobs, removedJobs],
  );
  const stale = useMemo(
    () => (loadedJobs?.stale ?? []).filter(job => !removedJobs.has(job.id)),
    [loadedJobs, removedJobs],
  );

  // A message from a later action replaces the load failure, as it always did.
  const [loadErrorDismissed, setLoadErrorDismissed] = useState(false);
  const notice = notification
    ?? (jobsError && !loadErrorDismissed ? { message: 'Failed to load jobs', type: 'error' as const } : null);

  useEffect(() => {
    if (jobsError) console.error('Failed to load jobs:', jobsError);
  }, [jobsError]);

  useEffect(() => {
    if (statsFailure) console.error('Failed to load job stats:', statsFailure);
  }, [statsFailure]);

  async function handleDelete(jobId: number) {
    try {
      const response = await fetch(`/api/jobs/${jobId}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      // Functional update: a concurrent submit/delete on another row may
      // still be in flight, so filtering against a captured `jobs` snapshot
      // would resurrect whatever that other request already removed.
      setRemovedJobs(prev => new Set(prev).add(jobId));
      setNotification({ message: 'Job dismissed', type: 'success' });
      // Dismissing a job changes the open-category counts; refresh so the
      // chart/table don't keep showing the pre-dismissal snapshot.
      loadStats();
    } catch (error) {
      console.error('Failed to delete job:', error);
      setNotification({ message: 'Failed to dismiss job', type: 'error' });
    }
  }

  async function handleSubmit(jobId: number) {
    if (submittingJobs.has(jobId)) {
      return;
    }
    setSubmittingJobs(prev => new Set(prev).add(jobId));
    try {
      const response = await fetch(`/api/jobs/${jobId}/submissions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      // Same reasoning as handleDelete: use the latest state, not a snapshot
      // captured when this handler started.
      setRemovedJobs(prev => new Set(prev).add(jobId));
      setNotification({ message: 'Job marked as submitted', type: 'success' });
      // A submission moves the job out of the open-category counts and into
      // submissions_per_day; refresh so the stats panel reflects that.
      loadStats();
    } catch (error) {
      console.error('Failed to submit job:', error);
      setNotification({ message: 'Failed to submit job', type: 'error' });
    } finally {
      setSubmittingJobs(prev => {
        const next = new Set(prev);
        next.delete(jobId);
        return next;
      });
    }
  }

  async function handleQueuePrompt(jobId: number) {
    setQueuingPrompts(prev => new Set(prev).add(jobId));
    try {
      const response = await fetch(`/api/jobs/${jobId}/agent-prompt`, {
        method: 'POST',
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({ detail: 'Unknown error' }));
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      setNotification({ message: 'Agent prompt queued successfully', type: 'success' });
    } catch (error) {
      console.error('Failed to queue agent prompt:', error);
      const message = error instanceof Error ? error.message : 'Failed to queue agent prompt';
      setNotification({ message, type: 'error' });
    } finally {
      setQueuingPrompts(prev => {
        const next = new Set(prev);
        next.delete(jobId);
        return next;
      });
    }
  }

  // The top-25 limit applies to fresh listings only; older ones, once shown,
  // render in their own section so the limit can never hide them.
  const shownJobs = showAll ? jobs : jobs.slice(0, TOP_LISTINGS);

  // A flat list in server rank order (#101679546294759424): grouping by
  // company would pull a low-fit listing above a better one elsewhere.
  function renderRows(list: Job[]) {
    return (
      <div className="company-group">
        <div className="jobs-list">
          {list.map(job => (
            <div key={job.id} className="job-row">
              <div className="job-main">
                <a
                  href={job.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="job-title"
                >
                  {job.title}
                </a>
                <span className="job-company">{job.company}</span>
                {job.location && (
                  <span className="job-location">{job.location}</span>
                )}
                {job.posted_date && (
                  <span className="job-date">{job.posted_date}</span>
                )}
                <span className="job-category">{job.category}</span>
              </div>
              <div className="job-actions">
                <button
                  className="job-agent-btn"
                  onClick={() => handleQueuePrompt(job.id)}
                  disabled={queuingPrompts.has(job.id)}
                  aria-label={`Queue agent prompt for ${job.title}`}
                  title="Queue agent to tailor resume and cover letter"
                >
                  {queuingPrompts.has(job.id) ? '...' : '🤖'}
                </button>
                <button
                  className="job-submit-btn"
                  onClick={() => handleSubmit(job.id)}
                  disabled={submittingJobs.has(job.id)}
                  aria-label={`Mark ${job.title} as submitted`}
                  title="Record that you submitted to this job"
                >
                  {submittingJobs.has(job.id) ? '...' : 'Submitted'}
                </button>
                <button
                  className="job-delete-btn"
                  onClick={() => handleDelete(job.id)}
                  aria-label={`Dismiss ${job.title}`}
                >
                  ×
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <PageShell title="Jobs" subtitle="Open job listings">
      {notice && (
        <Notification
          message={notice.message}
          type={notice.type}
          onClose={() => {
            setNotification(null);
            setLoadErrorDismissed(true);
          }}
        />
      )}

      {stats && !statsError && (
        <>
          <JobStatsChart stats={stats} />
          <CategoryTable categories={stats.categories} />
        </>
      )}

      {loading ? (
        <div className="jobs-loading">
          <Spinner size="large" />
        </div>
      ) : jobs.length === 0 && stale.length === 0 ? (
        <div className="jobs-empty">
          <p>No open job listings</p>
        </div>
      ) : (
        <div className="jobs-container">
          <RankedListControls
            shownCount={shownJobs.length}
            totalCount={jobs.length}
            staleCount={stale.length}
            showStale={showStale}
            onToggleStale={() => setShowStale(prev => !prev)}
            onShowAll={() => setShowAll(true)}
          />
          {renderRows(shownJobs)}
          {showStale && stale.length > 0 && (
            <section className="older-listings" aria-label="Older listings">
              <h2 className="older-listings-heading">Older listings</h2>
              {renderRows(stale)}
            </section>
          )}
        </div>
      )}
    </PageShell>
  );
}
