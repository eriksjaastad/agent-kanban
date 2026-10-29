import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { PageShell } from './PageShell';
import { Spinner } from './Spinner';
import { Notification } from './Notification';
import { useRequest } from '../hooks/useRequest';
import './JobsPage.css';
import './RemindersPage.css';

// Reminders (#8282): things the user wants to remember they have, kept until
// they are built into a process, skill or tool. Agents add them with
// `pt reminders add`; the page adds, marks built in and removes.
interface Reminder {
  id: number;
  name: string;
  purpose: string | null;
  location: string | null;
  created_at: string;
  built_in_at: string | null;
}

interface RemindersData {
  active: Reminder[];
  built_in: Reminder[];
}

function dateOnly(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '';
}

function Location({ value }: { value: string }) {
  if (/^https?:\/\//.test(value)) {
    return (
      <a href={value} target="_blank" rel="noopener noreferrer" className="reminder-location">
        {value}
      </a>
    );
  }
  return <code className="reminder-location">{value}</code>;
}

async function errorDetail(response: Response): Promise<string | null> {
  const body = await response.json().catch(() => null);
  return body && typeof body.detail === 'string' ? body.detail : null;
}

export function RemindersPage() {
  // The server's list is the only source of rows: after a write the page
  // reloads it rather than moving rows itself.
  const { data, error: loadError, loading, reload } = useRequest<RemindersData>(async (signal) => {
    const response = await fetch('/api/reminders', { signal });
    if (!response.ok) {
      throw new Error((await errorDetail(response)) ?? `HTTP ${response.status}`);
    }
    return response.json();
  }, [], { timeoutMs: 15000 });
  // A row is locked while its own request is in flight, and every row while
  // the list reloads or a load error stands, since the rows may be stale.
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const locked = (id: number) => busy.has(id) || loading || loadError !== null;
  const [draft, setDraft] = useState({ name: '', purpose: '', location: '' });
  const [adding, setAdding] = useState(false);
  const [notification, setNotification] = useState<{ message: string; type: 'success' | 'error' } | null>(null);

  useEffect(() => {
    if (loadError) console.error('Failed to load reminders:', loadError);
  }, [loadError]);

  async function send(request: () => Promise<Response>, success: string, failure: string): Promise<boolean> {
    try {
      const response = await request();
      if (!response.ok) {
        const detail = await errorDetail(response);
        console.error(failure, `HTTP ${response.status}`, detail);
        setNotification({ message: detail ? `${failure}: ${detail}` : failure, type: 'error' });
        return false;
      }
      setNotification({ message: success, type: 'success' });
      return true;
    } catch (error) {
      console.error(failure, error);
      setNotification({ message: failure, type: 'error' });
      return false;
    } finally {
      // Reload whatever happened: a failed or dropped request may still have
      // changed the row (here or elsewhere), and only the server's list says.
      reload();
    }
  }

  async function act(reminder: Reminder, request: () => Promise<Response>, success: string, failure: string) {
    if (locked(reminder.id)) return;
    setBusy(prev => new Set(prev).add(reminder.id));
    await send(request, success, failure);
    setBusy(prev => {
      const next = new Set(prev);
      next.delete(reminder.id);
      return next;
    });
  }

  async function handleAdd(event: FormEvent) {
    event.preventDefault();
    if (adding || !draft.name.trim()) return;
    setAdding(true);
    const added = await send(() => fetch('/api/reminders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(draft),
    }), 'Reminder added', 'Failed to add reminder');
    if (added) setDraft({ name: '', purpose: '', location: '' });
    setAdding(false);
  }

  // Built-in reminders keep their purpose and location, and can be removed.
  function renderRow(reminder: Reminder) {
    return (
      <li key={reminder.id} className="job-row reminder-row">
        <div className="job-main">
          <span className="reminder-name">{reminder.name}</span>
          {reminder.purpose && <span className="job-location">{reminder.purpose}</span>}
          {reminder.location && <Location value={reminder.location} />}
          <span className="job-date">added {dateOnly(reminder.created_at)}</span>
          {reminder.built_in_at && <span className="job-date">built in {dateOnly(reminder.built_in_at)}</span>}
        </div>
        <div className="job-actions">
          {!reminder.built_in_at && (
            <button
              type="button"
              className="job-submit-btn"
              onClick={() => act(reminder,
                () => fetch(`/api/reminders/${reminder.id}/built-in`, { method: 'POST' }),
                'Marked built in', 'Failed to mark built in')}
              disabled={locked(reminder.id)}
              aria-label={`Mark ${reminder.name} built in`}
              title="Its content now lives in a process, skill or tool"
            >
              Built in
            </button>
          )}
          <button
            type="button"
            className="job-delete-btn"
            onClick={() => act(reminder,
              () => fetch(`/api/reminders/${reminder.id}`, { method: 'DELETE' }),
              'Reminder removed', 'Failed to remove reminder')}
            disabled={locked(reminder.id)}
            aria-label={`Remove ${reminder.name}`}
          >
            ×
          </button>
        </div>
      </li>
    );
  }

  const active = data?.active ?? [];
  const builtIn = data?.built_in ?? [];

  return (
    <PageShell
      title="Reminders"
      subtitle="Tools, guides and ideas you have. Keep each one until it's built into a process, skill or tool."
    >
      {notification && (
        <Notification
          message={notification.message}
          type={notification.type}
          onClose={() => setNotification(null)}
        />
      )}

      {loadError && (
        <p className="reminders-error" role="status">
          {data === null ? 'Could not load reminders' : 'Could not refresh reminders; this list may be out of date'}
          {`: ${loadError.message}`}
          <button type="button" className="reminders-retry" onClick={reload} disabled={loading}>
            Retry
          </button>
        </p>
      )}

      {data === null ? (
        loadError === null && (
          <div className="jobs-loading">
            <Spinner size="large" />
          </div>
        )
      ) : (
        <div className="jobs-container">
          <section className="company-group" aria-label="Reminders">
            {active.length === 0 ? (
              <p className="reminders-empty">No reminders yet</p>
            ) : (
              <ul className="jobs-list reminders-list">
                {active.map(renderRow)}
              </ul>
            )}

            <form className="reminders-add" onSubmit={handleAdd} aria-label="Add a reminder">
              <input
                className="reminders-input"
                placeholder="Name"
                aria-label="Name"
                value={draft.name}
                onChange={e => setDraft({ ...draft, name: e.target.value })}
              />
              <input
                className="reminders-input reminders-input--wide"
                placeholder="Why or when to reach for it"
                aria-label="Why or when to reach for it"
                value={draft.purpose}
                onChange={e => setDraft({ ...draft, purpose: e.target.value })}
              />
              <input
                className="reminders-input reminders-input--wide"
                placeholder="Path or link"
                aria-label="Path or link"
                value={draft.location}
                onChange={e => setDraft({ ...draft, location: e.target.value })}
              />
              <button type="submit" className="job-submit-btn" disabled={adding || !draft.name.trim()}>
                Add
              </button>
            </form>
          </section>

          {builtIn.length > 0 && (
            <details className="company-group reminders-built-in">
              <summary>Built in ({builtIn.length})</summary>
              <ul className="jobs-list reminders-list">
                {builtIn.map(renderRow)}
              </ul>
            </details>
          )}
        </div>
      )}
    </PageShell>
  );
}
