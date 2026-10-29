import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageShell } from './PageShell';
import { Spinner } from './Spinner';
import { Notification } from './Notification';
import { useRequest } from '../hooks/useRequest';
import type { Job } from '../types';
import './JobsSubmittedPage.css';

interface Submission {
  id: number;
  job_id: number;
  submitted_at: string;
  resume_path: string | null;
  cover_letter_path: string | null;
  notes: string | null;
}

interface JobSubmissionGroup {
  job: Job;
  submissions: Submission[];
}

export function JobsSubmittedPage() {
  const [errorDismissed, setErrorDismissed] = useState(false);
  const { data, error, loading } = useRequest<JobSubmissionGroup[]>(async (signal) => {
    const response = await fetch('/api/jobs/submissions', { signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const body = await response.json();
    return body.jobs || [];
  }, []);
  const jobGroups = data ?? [];
  const notification = error && !errorDismissed ? { message: 'Failed to load submissions', type: 'error' as const } : null;

  useEffect(() => {
    if (error) console.error('Failed to load submissions:', error);
  }, [error]);

  function formatSubmittedDate(isoDate: string): string {
    try {
      const date = new Date(isoDate);
      return new Intl.DateTimeFormat('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      }).format(date);
    } catch {
      return isoDate;
    }
  }

  return (
    <PageShell title="Submissions" subtitle="Job application history">
      {notification && (
        <Notification
          message={notification.message}
          type={notification.type}
          onClose={() => setErrorDismissed(true)}
        />
      )}

      {loading ? (
        <div className="submissions-loading">
          <Spinner size="large" />
        </div>
      ) : jobGroups.length === 0 ? (
        <div className="submissions-empty">
          <p>No submissions yet</p>
          <p className="submissions-empty-hint">
            <Link to="/jobs">Browse open listings</Link> to get started
          </p>
        </div>
      ) : (
        <div className="submissions-container">
          {jobGroups.map(({ job, submissions }) => (
            <div key={job.id} className="submission-group">
              <div className="submission-job-header">
                <div className="submission-job-main">
                  <h2 className="submission-company">{job.company}</h2>
                  <a
                    href={job.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="submission-job-title"
                  >
                    {job.title}
                  </a>
                  {job.location && (
                    <span className="submission-job-location">{job.location}</span>
                  )}
                  <span className="submission-job-category">{job.category}</span>
                  {job.deleted_at && (
                    <span className="submission-job-dismissed" title="Dismissed from open listings">
                      Dismissed
                    </span>
                  )}
                </div>
                <div className="submission-count">
                  {submissions.length} {submissions.length === 1 ? 'submission' : 'submissions'}
                </div>
              </div>

              <div className="submissions-list">
                {submissions.map((submission) => (
                  <div key={submission.id} className="submission-item">
                    <div className="submission-date">
                      {formatSubmittedDate(submission.submitted_at)}
                    </div>
                    {submission.notes && (
                      <div className="submission-notes">{submission.notes}</div>
                    )}
                    {(submission.resume_path || submission.cover_letter_path) && (
                      <div className="submission-attachments">
                        {submission.resume_path && (
                          <span className="submission-attachment">Resume: {submission.resume_path}</span>
                        )}
                        {submission.cover_letter_path && (
                          <span className="submission-attachment">
                            Cover letter: {submission.cover_letter_path}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </PageShell>
  );
}
