import { useEffect, useState } from 'react';
import { PageShell } from './PageShell';
import { Spinner } from './Spinner';
import { Notification } from './Notification';
import { RankedListControls, TOP_LISTINGS } from './RankedListControls';
import { useRequest } from '../hooks/useRequest';
import './JobsPage.css';
import './VolunteerPage.css';

// Volunteer roles are `jobs` rows with kind='volunteer' (#101668605654102016).
// Dismiss and "Submitted" reuse the Jobs board's endpoints.
interface Role {
  id: number;
  company: string;
  title: string;
  url: string;
  location: string | null;
  posted_date: string | null;
  first_seen: string;
  volunteering_since: string | null;
  last_submitted_at?: string | null;
}

interface VolunteerData {
  migration_pending?: boolean;
  current: Role[];
  applied: Role[];
  possible: Role[];
  possible_stale: Role[];
}

const EMPTY: VolunteerData = { current: [], applied: [], possible: [], possible_stale: [] };

function dateOnly(iso: string | null | undefined): string {
  return iso ? iso.slice(0, 10) : '';
}

function RoleLink({ role }: { role: Role }) {
  return (
    <a href={role.url} target="_blank" rel="noopener noreferrer" className="job-title">
      {role.title}
    </a>
  );
}

export function VolunteerPage() {
  // The server's payload is the only source of the three sections: after a
  // write the page reloads it rather than moving rows itself, so a role
  // another tab already changed shows where the server keeps it, with the
  // server's dates. reload() aborts an older load, so a late response can
  // never overwrite a newer one.
  const { data: loaded, error: loadError, loading, reload } = useRequest<VolunteerData>(async (signal) => {
    const response = await fetch('/api/volunteer', { signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return { ...EMPTY, ...(await response.json()) };
  // A hung load would keep every role locked; after 15s it fails instead.
  }, [], { timeoutMs: 15000 });
  const data = loaded ?? EMPTY;
  // A role is locked while its own request is in flight. Every role is
  // locked while the list reloads (a successful write calls reload(), and
  // `loading` stays true until the newest load lands; older ones are
  // aborted) and while a load error stands, because the rows may then be
  // stale: a row a write already moved must not take a second click. Retry
  // is the way out of an error.
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const locked = (roleId: number) => busy.has(roleId) || loading || loadError !== null;
  const [showStale, setShowStale] = useState(false);
  const [showAll, setShowAll] = useState(false);
  // Action results only. A load failure is shown inline and stays until a
  // load succeeds, because the notification closes itself.
  const [notification, setNotification] = useState<{ message: string; type: 'success' | 'error' } | null>(null);

  useEffect(() => {
    if (loadError) console.error('Failed to load volunteer roles:', loadError);
  }, [loadError]);

  async function act(roleId: number, request: () => Promise<Response>, success: string, failure: string) {
    if (locked(roleId)) {
      return;
    }
    setBusy(prev => new Set(prev).add(roleId));
    try {
      const response = await request();
      if (!response.ok) {
        // The server's detail (e.g. 409 "run `pt db migrate`") says what to
        // do next; fall back to the generic failure text without one.
        const body = await response.json().catch(() => null);
        const detail = body && typeof body.detail === 'string' ? body.detail : null;
        console.error(failure, `HTTP ${response.status}`, detail);
        setNotification({ message: detail ? `${failure}: ${detail}` : failure, type: 'error' });
        return;
      }
      setNotification({ message: success, type: 'success' });
      reload();
    } catch (error) {
      console.error(failure, error);
      setNotification({ message: failure, type: 'error' });
    } finally {
      setBusy(prev => {
        const next = new Set(prev);
        next.delete(roleId);
        return next;
      });
    }
  }

  function handleDelete(role: Role) {
    act(role.id, () => fetch(`/api/jobs/${role.id}`, { method: 'DELETE' }),
      'Listing dismissed', 'Failed to dismiss listing');
  }

  function handleSubmitted(role: Role) {
    act(role.id, () => fetch(`/api/jobs/${role.id}/submissions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }), 'Marked as applied', 'Failed to mark as applied');
  }

  // From Applied or straight from Possible: a role the user already volunteers
  // at needs no fictitious application first.
  function handleStarted(role: Role) {
    act(role.id, () => fetch(`/api/jobs/${role.id}/volunteering`, { method: 'POST' }),
      'Marked as volunteering', 'Failed to mark as volunteering');
  }

  // The top-25 limit applies to fresh listings; older ones, once shown,
  // render in their own list so the limit can never hide them.
  const shownPossible = showAll ? data.possible : data.possible.slice(0, TOP_LISTINGS);

  function renderPossibleRow(role: Role) {
    return (
      <div key={role.id} className="job-row">
        <div className="job-main">
          <RoleLink role={role} />
          <span className="job-location">{role.company}</span>
          {role.location && <span className="job-location">{role.location}</span>}
          {role.posted_date && <span className="job-date">{role.posted_date}</span>}
        </div>
        <div className="job-actions">
          <button
            className="job-submit-btn"
            onClick={() => handleStarted(role)}
            disabled={locked(role.id)}
            aria-label={`Started volunteering at ${role.company}`}
            title="Record that you already volunteer here"
          >
            {locked(role.id) ? '...' : 'Started volunteering'}
          </button>
          <button
            className="job-submit-btn"
            onClick={() => handleSubmitted(role)}
            disabled={locked(role.id)}
            aria-label={`Mark ${role.title} as submitted`}
            title="Record that you applied"
          >
            {locked(role.id) ? '...' : 'Submitted'}
          </button>
          <button
            className="job-delete-btn"
            onClick={() => handleDelete(role)}
            disabled={locked(role.id)}
            aria-label={`Dismiss ${role.title}`}
          >
            ×
          </button>
        </div>
      </div>
    );
  }

  return (
    <PageShell title="Volunteer" subtitle="Where you volunteer, where you applied, and what to look at next">
      {notification && (
        <Notification
          message={notification.message}
          type={notification.type}
          onClose={() => setNotification(null)}
        />
      )}

      {loadError && (
        <p className="volunteer-error" role="status">
          {loaded === null
            ? 'Could not load volunteer roles.'
            : 'Could not refresh volunteer roles; this list may be out of date.'}
          {' '}
          <button type="button" className="volunteer-retry" onClick={reload} disabled={loading}>
            Retry
          </button>
        </p>
      )}

      {loaded === null ? (
        loadError === null && (
          <div className="jobs-loading">
            <Spinner size="large" />
          </div>
        )
      ) : (
        <div className="jobs-container">
          {data.migration_pending && (
            <p className="volunteer-empty">
              Volunteer roles need a database update: run <code>pt db migrate</code>.
            </p>
          )}
          <section className="company-group" aria-label="Currently volunteering">
            <h2 className="company-name">Currently volunteering</h2>
            {data.current.length === 0 ? (
              <p className="volunteer-empty">Not volunteering anywhere yet</p>
            ) : (
              <div className="jobs-list">
                {data.current.map(role => (
                  <div key={role.id} className="job-row">
                    <div className="job-main">
                      <RoleLink role={role} />
                      <span className="job-location">{role.company}</span>
                      {role.volunteering_since && (
                        <span className="job-date">since {dateOnly(role.volunteering_since)}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="company-group" aria-label="Applied">
            <h2 className="company-name">Applied</h2>
            {data.applied.length === 0 ? (
              <p className="volunteer-empty">No applications yet</p>
            ) : (
              <div className="jobs-list">
                {data.applied.map(role => (
                  <div key={role.id} className="job-row">
                    <div className="job-main">
                      <RoleLink role={role} />
                      <span className="job-location">{role.company}</span>
                      {role.last_submitted_at && (
                        <span className="job-date">applied {dateOnly(role.last_submitted_at)}</span>
                      )}
                    </div>
                    <div className="job-actions">
                      <button
                        className="job-submit-btn"
                        onClick={() => handleStarted(role)}
                        disabled={locked(role.id)}
                        aria-label={`Started volunteering at ${role.company}`}
                      >
                        {locked(role.id) ? '...' : 'Started volunteering'}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="company-group" aria-label="Possible">
            <h2 className="company-name">Possible</h2>
            <RankedListControls
              shownCount={shownPossible.length}
              totalCount={data.possible.length}
              staleCount={data.possible_stale.length}
              showStale={showStale}
              onToggleStale={() => setShowStale(prev => !prev)}
              onShowAll={() => setShowAll(true)}
            />
            {data.possible.length === 0 ? (
              <p className="volunteer-empty">No open volunteer listings</p>
            ) : (
              <div className="jobs-list">{shownPossible.map(renderPossibleRow)}</div>
            )}
            {showStale && data.possible_stale.length > 0 && (
              <div className="older-listings" aria-label="Older volunteer listings" role="region">
                <h3 className="older-listings-heading">Older listings</h3>
                <div className="jobs-list">{data.possible_stale.map(renderPossibleRow)}</div>
              </div>
            )}
          </section>
        </div>
      )}
    </PageShell>
  );
}
