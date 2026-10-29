import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import './OutreachPanel.css';

interface OutreachContact {
  id: number;
  name: string;
  contacted_at: string | null;
  created_at: string;
}

// Per-field (per-contact-name) save status, independent of the panel-wide
// request queue. `value` is the field's unsaved/displayed value while a save
// is pending or has failed — the source of truth for rendering takes this
// over the server's `contact.name` whenever an entry exists here, so a failed
// or in-flight rename is never silently overwritten by a resync or by another
// row's action. `seq` guards against a stale response or a stale fade timer
// (from an earlier save of the same field) clobbering a later one.
interface FieldSaveState {
  value: string;
  status: 'saving' | 'saved' | 'error';
  seq: number;
  fading?: boolean;
}

const SAVED_DISPLAY_MS = 3000;
const FADE_MS = 300;

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

const MINIMIZED_STORAGE_KEY = 'outreach-panel:minimized';

function readMinimized(): boolean {
  try {
    return window.localStorage.getItem(MINIMIZED_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function writeMinimized(minimized: boolean): void {
  try {
    window.localStorage.setItem(MINIMIZED_STORAGE_KEY, minimized ? '1' : '0');
  } catch {
    // Local storage is unavailable; the panel still works for this session.
  }
}

function errorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
  }
  return fallback;
}

// Ensures a message reads as a complete sentence, without doubling a period
// the caller already supplied.
function asSentence(text: string): string {
  return text.endsWith('.') ? text : `${text}.`;
}

export const OUTREACH_REQUEST_TIMEOUT_MS = 20000;
const OUTREACH_TIMEOUT_MESSAGE = 'Request timed out — reloading contacts';

// Every contacts request goes through this helper so a fetch that never settles
// (server hang, stalled connection, laptop sleep mid-request) cannot block the
// panel-wide queue forever. The controller aborts after the timeout, the timer
// is always cleared, and an abort caused by the timeout surfaces as the
// ordinary timeout error the call sites already know how to display.
function fetchWithTimeout(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), OUTREACH_REQUEST_TIMEOUT_MS);
  try {
    return fetch(input, { ...init, signal: controller.signal })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          throw new Error(OUTREACH_TIMEOUT_MESSAGE);
        }
        throw error;
      })
      .finally(() => clearTimeout(timer));
  } catch (error) {
    clearTimeout(timer);
    throw error;
  }
}

export function OutreachPanel() {
  const [contacts, setContacts] = useState<OutreachContact[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [minimized, setMinimized] = useState<boolean>(() => readMinimized());
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingValue, setEditingValue] = useState('');
  const editingIdRef = useRef<number | null>(null);
  editingIdRef.current = editingId;

  // ONE panel-wide request queue. Every server interaction (the initial load,
  // add, rename, contacted, replied, delete, and the post-error resync GET) is
  // enqueued here and runs strictly one after another. queueRef holds a promise
  // chain; each new task is appended with `.then(run, run)` so a failed task can
  // never break the chain for the tasks queued behind it.
  const queueRef = useRef<Promise<void>>(Promise.resolve());
  const [queueDepth, setQueueDepth] = useState(0);

  // Rows with a queued/in-flight Contacted / Replied / Delete action. Used to
  // de-duplicate accidental double-clicks and to disable the row's buttons.
  const [busyRows, setBusyRows] = useState<Set<number>>(() => new Set());
  const busyRowsRef = useRef<Set<number>>(new Set());

  // Rows with a queued/in-flight rename. Ordering between a rename and another
  // action on the same row is guaranteed by the queue, but the row still cannot
  // enter edit mode again until its rename has landed.
  const [renamingRows, setRenamingRows] = useState<Set<number>>(() => new Set());
  const renamingRowsRef = useRef<Set<number>>(new Set());

  // The add form can only have one queued/in-flight request at a time.
  const [addInFlight, setAddInFlight] = useState(false);
  const addInFlightRef = useRef(false);

  // Per-contact-id save status for the name field (see FieldSaveState above).
  const [fieldStates, setFieldStates] = useState<Map<number, FieldSaveState>>(() => new Map());
  const fieldStatesRef = useRef<Map<number, FieldSaveState>>(new Map());
  // The latest sequence number issued per contact id, so a stale response or a
  // stale fade timer from an earlier save of the same field can recognize
  // itself as stale and no-op instead of clobbering a later save.
  const latestSeqRef = useRef<Map<number, number>>(new Map());
  const seqCounterRef = useRef(0);
  // "Saved" fade/clear timers per contact id, so a new save can cancel the
  // previous save's pending timers outright (belt-and-suspenders alongside
  // the seq check above).
  const fieldTimersRef = useRef<Map<number, ReturnType<typeof setTimeout>[]>>(new Map());

  useEffect(
    () => () => {
      fieldTimersRef.current.forEach((timers) => timers.forEach((timer) => clearTimeout(timer)));
    },
    []
  );

  const enqueue = useCallback((task: () => Promise<void>): Promise<void> => {
    setQueueDepth((depth) => depth + 1);
    const run = async (): Promise<void> => {
      try {
        await task();
      } finally {
        setQueueDepth((depth) => depth - 1);
      }
    };
    // The rejection handler also runs the next task, so one failed task can
    // never break the chain. Tasks handle their own errors internally, so run
    // only rejects if something truly unexpected escapes.
    const next = queueRef.current.then(run, run);
    queueRef.current = next;
    return next;
  }, []);

  function setRowBusy(id: number, busy: boolean): void {
    const next = new Set(busyRowsRef.current);
    if (busy) {
      next.add(id);
    } else {
      next.delete(id);
    }
    busyRowsRef.current = next;
    setBusyRows(next);
  }

  function setRowRenaming(id: number, renaming: boolean): void {
    const next = new Set(renamingRowsRef.current);
    if (renaming) {
      next.add(id);
    } else {
      next.delete(id);
    }
    renamingRowsRef.current = next;
    setRenamingRows(next);
  }

  function setFieldState(id: number, state: FieldSaveState | null): void {
    const next = new Map(fieldStatesRef.current);
    if (state === null) {
      next.delete(id);
    } else {
      next.set(id, state);
    }
    fieldStatesRef.current = next;
    setFieldStates(next);
  }

  function clearFieldTimers(id: number): void {
    const timers = fieldTimersRef.current.get(id);
    if (timers) {
      timers.forEach((timer) => clearTimeout(timer));
      fieldTimersRef.current.delete(id);
    }
  }

  // Drops a field's unsaved value entirely, reverting display to the server's
  // name. Used only when the row itself no longer exists (404) or can no
  // longer be renamed (409, replied elsewhere) — never for a
  // transient failure, which must keep the typed value and offer retry.
  function dropField(id: number): void {
    clearFieldTimers(id);
    setFieldState(id, null);
  }

  // Schedules the "Saved" indicator's fade-out and clear. Both timers check
  // the seq against latestSeqRef before acting, so if a newer save of the
  // same field started in the meantime, this stale timer no-ops instead of
  // clobbering the newer save's state.
  function scheduleFade(id: number, seq: number): void {
    clearFieldTimers(id);
    const timers: ReturnType<typeof setTimeout>[] = [];
    const clear = () => {
      if (latestSeqRef.current.get(id) === seq) {
        setFieldState(id, null);
      }
    };
    if (prefersReducedMotion()) {
      timers.push(setTimeout(clear, SAVED_DISPLAY_MS));
    } else {
      timers.push(
        setTimeout(() => {
          if (latestSeqRef.current.get(id) !== seq) return;
          const current = fieldStatesRef.current.get(id);
          if (current) setFieldState(id, { ...current, fading: true });
        }, SAVED_DISPLAY_MS - FADE_MS)
      );
      timers.push(setTimeout(clear, SAVED_DISPLAY_MS));
    }
    fieldTimersRef.current.set(id, timers);
  }

  function displayName(contact: OutreachContact): string {
    return fieldStates.get(contact.id)?.value ?? contact.name;
  }

  // Stable callback ref: React only invokes it when the edit input mounts or
  // unmounts, so focus + select happen once when a row enters edit mode instead
  // of on every render (which re-selected the text after each keystroke).
  const focusEditInput = useCallback((el: HTMLInputElement | null) => {
    if (el) {
      el.focus();
      el.select();
    }
  }, []);

  useEffect(() => {
    void enqueue(async () => {
      try {
        const response = await fetchWithTimeout('/api/outreach/contacts');
        if (!response.ok) {
          const payload = await response.json().catch(() => null);
          throw new Error(errorMessage(payload, `Failed to load contacts (HTTP ${response.status})`));
        }
        const data = await response.json();
        setContacts(data.contacts || []);
        setError(null);
      } catch (err) {
        console.error('Failed to load outreach contacts:', err);
        setError(err instanceof Error ? err.message : 'Failed to load contacts');
      } finally {
        setLoading(false);
      }
    });
  }, [enqueue]);

  // After a failed action the server is the only source of truth: show the
  // error, then re-fetch and render the server's list so the UI can never stay
  // out of step with the DB. The GET is itself enqueued, so by the time it runs
  // no other request is in flight and its full replace is truthful. If the
  // reload also fails, keep the last list and say so in the error.
  const resyncAfterError = useCallback(
    (message: string): void => {
      void enqueue(async () => {
        try {
          const response = await fetchWithTimeout('/api/outreach/contacts');
          if (!response.ok) {
            const payload = await response.json().catch(() => null);
            throw new Error(
              errorMessage(payload, `Failed to reload contacts (HTTP ${response.status})`)
            );
          }
          const data = await response.json();
          setContacts(data.contacts || []);
        } catch (err) {
          console.error('Failed to reload outreach contacts after an error:', err);
          const reloadMessage = err instanceof Error ? err.message : 'Failed to reload contacts';
          setError(`${asSentence(message)} Could not reload contacts: ${asSentence(reloadMessage)}`);
        }
      });
    },
    [enqueue]
  );

  function handleAdd(event: FormEvent): void {
    event.preventDefault();
    if (addInFlightRef.current) return;
    const trimmed = name.trim();
    if (!trimmed) return;
    addInFlightRef.current = true;
    setAddInFlight(true);
    void enqueue(async () => {
      try {
        const response = await fetchWithTimeout('/api/outreach/contacts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: trimmed }),
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => null);
          throw new Error(errorMessage(payload, `Failed to add contact (HTTP ${response.status})`));
        }
        const data = await response.json();
        setContacts((prev) => [...(prev ?? []), data.contact]);
        setName('');
        setError(null);
      } catch (err) {
        console.error('Failed to add outreach contact:', err);
        const message = err instanceof Error ? err.message : 'Failed to add contact';
        setError(message);
        resyncAfterError(message);
      } finally {
        addInFlightRef.current = false;
        setAddInFlight(false);
      }
    });
  }

  function startEdit(contact: OutreachContact) {
    if (busyRowsRef.current.has(contact.id) || renamingRowsRef.current.has(contact.id)) return;
    editingIdRef.current = contact.id;
    setEditingId(contact.id);
    setEditingValue(displayName(contact));
    setError(null);
  }

  function cancelEdit() {
    editingIdRef.current = null;
    setEditingId(null);
  }

  function commitEdit() {
    const id = editingIdRef.current;
    if (id === null) return;
    const nextName = editingValue.trim();
    if (!nextName) {
      cancelEdit();
      return;
    }
    editingIdRef.current = null;
    setEditingId(null);
    startRename(id, nextName);
  }

  // Kicks off a rename: shows "Saving…" for this field immediately (the field
  // is considered saving from the moment it's queued, not just once in
  // flight), then enqueues the PATCH on the panel-wide queue like every other
  // action. `seq` is this attempt's sequence number for this contact id —
  // handleRenameSuccess/Failure and scheduleFade's timers all check it against
  // latestSeqRef before acting, so an outcome or timer from an earlier attempt
  // can never clobber a later one for the same field.
  function startRename(id: number, nextName: string): void {
    const seq = seqCounterRef.current + 1;
    seqCounterRef.current = seq;
    latestSeqRef.current.set(id, seq);
    clearFieldTimers(id);
    setFieldState(id, { value: nextName, status: 'saving', seq });
    setRowRenaming(id, true);
    void enqueue(async () => {
      try {
        const response = await fetchWithTimeout(`/api/outreach/contacts/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nextName }),
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => null);
          const message = errorMessage(payload, `Failed to rename contact (HTTP ${response.status})`);
          handleRenameFailure(id, seq, nextName, message, response.status);
          return;
        }
        const data = await response.json();
        handleRenameSuccess(id, seq, data.contact);
      } catch (err) {
        console.error('Failed to rename outreach contact:', err);
        const message = err instanceof Error ? err.message : 'Failed to rename contact';
        handleRenameFailure(id, seq, nextName, message, null);
      } finally {
        setRowRenaming(id, false);
      }
    });
  }

  function handleRenameSuccess(id: number, seq: number, contact: OutreachContact): void {
    if (latestSeqRef.current.get(id) !== seq) return; // a later save has already superseded this response
    setContacts((prev) => (prev ?? []).map((c) => (c.id === id ? contact : c)));
    setError(null);
    setFieldState(id, { value: contact.name, status: 'saved', seq });
    scheduleFade(id, seq);
  }

  // 404 (row deleted) and 409 (row replied meanwhile, can no longer be
  // renamed) are the only cases where the unsaved value is dropped — the
  // row itself is gone or truthfully can't take this edit, so there's nothing
  // to retry. Every other failure (500, network error, timeout) keeps the
  // typed value and offers retry via the field's error state.
  function handleRenameFailure(
    id: number,
    seq: number,
    attemptedName: string,
    message: string,
    status: number | null
  ): void {
    if (latestSeqRef.current.get(id) !== seq) return; // a later save has already superseded this response
    if (status === 404 || status === 409) {
      dropField(id);
    } else {
      setFieldState(id, { value: attemptedName, status: 'error', seq });
    }
    setError(message);
    resyncAfterError(message);
  }

  function retryRename(id: number): void {
    const current = fieldStatesRef.current.get(id);
    if (!current || current.status !== 'error') return;
    startRename(id, current.value);
  }

  function handleContacted(id: number): void {
    if (busyRowsRef.current.has(id)) return;
    setRowBusy(id, true);
    void enqueue(async () => {
      try {
        const response = await fetchWithTimeout(`/api/outreach/contacts/${id}/contacted`, {
          method: 'POST',
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => null);
          throw new Error(errorMessage(payload, `Failed to mark contact (HTTP ${response.status})`));
        }
        const data = await response.json();
        setContacts((prev) =>
          (prev ?? []).map((contact) => (contact.id === id ? data.contact : contact))
        );
        setError(null);
      } catch (err) {
        console.error('Failed to mark outreach contact as contacted:', err);
        const message = err instanceof Error ? err.message : 'Failed to mark contact';
        setError(message);
        resyncAfterError(message);
      } finally {
        setRowBusy(id, false);
      }
    });
  }

  function handleReplied(id: number): void {
    if (busyRowsRef.current.has(id)) return;
    setRowBusy(id, true);
    void enqueue(async () => {
      try {
        const response = await fetchWithTimeout(`/api/outreach/contacts/${id}/replied`, {
          method: 'POST',
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => null);
          throw new Error(errorMessage(payload, `Failed to mark reply (HTTP ${response.status})`));
        }
        setContacts((prev) => (prev ?? []).filter((contact) => contact.id !== id));
        dropField(id);
        setError(null);
      } catch (err) {
        console.error('Failed to mark outreach contact as replied:', err);
        const message = err instanceof Error ? err.message : 'Failed to mark reply';
        setError(message);
        resyncAfterError(message);
      } finally {
        setRowBusy(id, false);
      }
    });
  }

  function handleDelete(id: number): void {
    if (busyRowsRef.current.has(id)) return;
    setRowBusy(id, true);
    void enqueue(async () => {
      try {
        const response = await fetchWithTimeout(`/api/outreach/contacts/${id}`, {
          method: 'DELETE',
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => null);
          throw new Error(errorMessage(payload, `Failed to delete contact (HTTP ${response.status})`));
        }
        setContacts((prev) => (prev ?? []).filter((contact) => contact.id !== id));
        dropField(id);
        setError(null);
      } catch (err) {
        console.error('Failed to delete outreach contact:', err);
        const message = err instanceof Error ? err.message : 'Failed to delete contact';
        setError(message);
        resyncAfterError(message);
      } finally {
        setRowBusy(id, false);
      }
    });
  }

  function toggleMinimized() {
    const next = !minimized;
    setMinimized(next);
    writeMinimized(next);
  }

  const uncontacted = (contacts ?? []).filter((contact) => contact.contacted_at === null);
  const contacted = (contacts ?? []).filter((contact) => contact.contacted_at !== null);
  const queueBusy = queueDepth > 0;

  // True while any field has a save queued/in-flight or failed-and-unretried.
  // "saved" (already landed on the server) does not count, even while its
  // transient indicator is still visible.
  const hasUnsavedWork = useMemo(
    () => Array.from(fieldStates.values()).some((state) => state.status === 'saving' || state.status === 'error'),
    [fieldStates]
  );

  useEffect(() => {
    if (!hasUnsavedWork) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [hasUnsavedWork]);

  function renderFieldStatus(id: number) {
    const state = fieldStates.get(id);
    if (!state) return null;
    return (
      <span
        className={`outreach-status${state.fading ? ' outreach-status--fade' : ''}`}
        role="status"
        aria-live="polite"
      >
        {state.status === 'saving' && 'Saving…'}
        {state.status === 'saved' && 'Saved'}
        {state.status === 'error' && (
          <button type="button" className="outreach-status-retry" onClick={() => retryRename(id)}>
            {"Couldn't save — retry"}
          </button>
        )}
      </span>
    );
  }

  // Every active row's name, contacted or not, is click-to-edit: clicking
  // swaps it for an input, and blur (or Enter) saves and swaps back.
  function renderNameCell(contact: OutreachContact) {
    return (
      <div className="outreach-name-cell">
        {editingId === contact.id ? (
          <input
            type="text"
            className="outreach-edit-input"
            value={editingValue}
            onChange={(event) => setEditingValue(event.target.value)}
            onBlur={commitEdit}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                commitEdit();
              } else if (event.key === 'Escape') {
                cancelEdit();
              }
            }}
            ref={focusEditInput}
            aria-label={`Edit name for ${displayName(contact)}`}
          />
        ) : (
          <>
            <button
              type="button"
              className="outreach-name"
              onClick={() => startEdit(contact)}
              disabled={busyRows.has(contact.id) || renamingRows.has(contact.id)}
            >
              {displayName(contact)}
            </button>
            {renderFieldStatus(contact.id)}
          </>
        )}
      </div>
    );
  }

  return (
    <section className="outreach-panel" aria-label="People to contact">
      <h2 className="outreach-header">
        <button
          type="button"
          className="outreach-toggle"
          onClick={toggleMinimized}
          aria-expanded={!minimized}
        >
          <span className="outreach-chevron" aria-hidden="true">
            {minimized ? '▸' : '▾'}
          </span>
          People to contact
        </button>
        {queueBusy && <span className="outreach-saving">Saving…</span>}
      </h2>

      {!minimized && (
        <div className="outreach-body">
          {error && (
            <p role="alert" className="outreach-error">
              {error}
            </p>
          )}

          {loading && contacts === null ? (
            <p className="outreach-loading">Loading contacts...</p>
          ) : (
            <>
              {uncontacted.length > 0 && (
                <ul className="outreach-list" aria-label="Not contacted">
                  {uncontacted.map((contact) => (
                    <li key={contact.id} className="outreach-row">
                      {renderNameCell(contact)}
                      <button
                        type="button"
                        className="outreach-action"
                        onClick={() => handleContacted(contact.id)}
                        disabled={busyRows.has(contact.id)}
                      >
                        Contacted
                      </button>
                      <button
                        type="button"
                        className="outreach-action outreach-action--danger"
                        onClick={() => handleDelete(contact.id)}
                        disabled={busyRows.has(contact.id)}
                      >
                        Delete
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              <form className="outreach-add" onSubmit={handleAdd}>
                <input
                  type="text"
                  className="outreach-input"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Name or email"
                  aria-label="Name or email"
                />
                <button type="submit" className="outreach-add-btn" disabled={addInFlight}>
                  Add
                </button>
              </form>

              {contacted.length > 0 && (
                <ul className="outreach-list" aria-label="Contacted">
                  {contacted.map((contact) => (
                    <li key={contact.id} className="outreach-row outreach-row--contacted">
                      {renderNameCell(contact)}
                      <button
                        type="button"
                        className="outreach-action"
                        onClick={() => handleReplied(contact.id)}
                        disabled={busyRows.has(contact.id)}
                      >
                        Replied
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
