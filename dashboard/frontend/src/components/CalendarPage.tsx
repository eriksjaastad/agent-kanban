import { useEffect, useState } from 'react';
import {
  fetchCalendarEvents,
  fetchCalendarCrons,
  fetchCalendarToday,
  createCalendarEvent,
  markCalendarEventDone,
  fetchProjects,
} from '../api';
import type {
  CalendarEvent,
  CalendarCronJob,
  CreateCalendarEventPayload,
} from '../api';
import { useRequest } from '../hooks/useRequest';
import { PageShell } from './PageShell';
import { ScheduledJobsPanel } from './ScheduledJobsPanel';
import './CalendarPage.css';

// ── helpers ──────────────────────────────────────────────────────────────────

const EVENT_TYPE_EMOJI: Record<string, string> = {
  reminder: '🔔',
  deadline: '🚨',
  milestone: '🏁',
  meeting: '🤝',
  recurring: '🔁',
};

const MACHINE_COLOR: Record<string, string> = {
  MacBook: 'var(--cal-mac)',
  OpenClaw: 'var(--cal-claw)',
  Both: 'var(--cal-both)',
  web: 'var(--cal-web)',
};

function formatDate(iso: string): string {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatDateShort(iso: string): string {
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function getDaysInMonth(year: number, month: number): Date[] {
  const days: Date[] = [];
  const firstDay = new Date(year, month, 1);
  const lastDay = new Date(year, month + 1, 0);
  // Pad start
  for (let i = 0; i < firstDay.getDay(); i++) {
    days.push(new Date(year, month, -i));
  }
  days.reverse();
  // Actual days
  for (let d = 1; d <= lastDay.getDate(); d++) {
    days.push(new Date(year, month, d));
  }
  // Pad end to complete grid
  const remaining = 42 - days.length;
  for (let i = 1; i <= remaining; i++) {
    days.push(new Date(year, month + 1, i));
  }
  return days;
}

function isoDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

// ── component ─────────────────────────────────────────────────────────────────

export function CalendarPage() {
  const now = new Date();
  const [viewYear, setViewYear] = useState(now.getFullYear());
  const [viewMonth, setViewMonth] = useState(now.getMonth());
  // Events, crons and projects load together, as one all-or-nothing request:
  // any failure shows its error at once, and the error is hidden while a
  // reload (after Mark Done or Add Event) is in flight.
  const calendar = useRequest(
    async (signal) => {
      const [events, crons, projects, todays] = await Promise.all([
        fetchCalendarEvents({ days: 120, include_all: true }, signal),
        fetchCalendarCrons(undefined, signal),
        fetchProjects(signal),
        fetchCalendarToday(signal),
      ]);
      return { events, crons, todays, projects: projects.slice().sort((a, b) => a.name.localeCompare(b.name)) };
    },
    [],
  );
  const events: CalendarEvent[] = calendar.data?.events ?? [];
  const crons: CalendarCronJob[] = calendar.data?.crons ?? [];
  const projects = calendar.data?.projects ?? [];
  const todays = calendar.data?.todays;
  const { loading } = calendar;
  const error = loading ? null : calendar.error?.message ?? null;
  const load = calendar.reload;
  const [selectedEvent, setSelectedEvent] = useState<CalendarEvent | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [filterProject, setFilterProject] = useState('');
  const [filterType, setFilterType] = useState('');
  // Today is the default: what's next matters more than the month (#8793).
  const [activeTab, setActiveTab] = useState<'today' | 'calendar' | 'crons'>('today');

  // Add-event form state
  const [formTitle, setFormTitle] = useState('');
  const [formDate, setFormDate] = useState('');
  const [formTime, setFormTime] = useState('');
  const [formType, setFormType] = useState('reminder');
  const [formProject, setFormProject] = useState('');
  const [formMachine, setFormMachine] = useState('');
  const [formDescription, setFormDescription] = useState('');
  const [formPrompt, setFormPrompt] = useState('');
  const [formRecurrence, setFormRecurrence] = useState('');
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [markDoneError, setMarkDoneError] = useState<string | null>(null);

  // One filter for every view: the header's project and type selects.
  const matchesFilters = (ev: CalendarEvent) =>
    ev.status === 'active'
    && (!filterProject || ev.project_id === filterProject)
    && (!filterType || ev.event_type === filterType);
  const visibleEvents = events.filter(matchesFilters);
  const todaysEvents = todays?.events.filter(matchesFilters) ?? [];

  // Today is the server's date, so ask the server (each minute and on focus)
  // and reload once its date moves on, e.g. a page left open overnight. No
  // check runs while a load is in flight, and the first new date retires the
  // rest, so a slow reload is never restarted.
  useEffect(() => {
    if (!todays || loading) return;
    let live = true;
    const check = () => {
      fetchCalendarToday().then(
        (t) => { if (live && t.date !== todays.date) { live = false; load(); } },
        (err) => console.error('calendar date check failed', err),
      );
    };
    const timer = window.setInterval(check, 60_000);
    window.addEventListener('focus', check);
    return () => { live = false; window.clearInterval(timer); window.removeEventListener('focus', check); };
  }, [todays, loading, load]);

  // Map events by date for the grid
  const eventsByDate = new Map<string, CalendarEvent[]>();
  for (const ev of visibleEvents) {
    const list = eventsByDate.get(ev.event_date) ?? [];
    list.push(ev);
    eventsByDate.set(ev.event_date, list);
  }

  // Upcoming: next 14 days
  const todayStr = today();
  const twoWeeksOut = new Date(todayStr);
  twoWeeksOut.setDate(twoWeeksOut.getDate() + 14);
  const twoWeeksStr = isoDate(twoWeeksOut);
  const upcoming = visibleEvents
    .filter(ev => ev.event_date >= todayStr && ev.event_date <= twoWeeksStr)
    .sort((a, b) => a.event_date.localeCompare(b.event_date));

  const days = getDaysInMonth(viewYear, viewMonth);

  function prevMonth() {
    if (viewMonth === 0) { setViewYear(y => y - 1); setViewMonth(11); }
    else setViewMonth(m => m - 1);
  }
  function nextMonth() {
    if (viewMonth === 11) { setViewYear(y => y + 1); setViewMonth(0); }
    else setViewMonth(m => m + 1);
  }

  function handleDayClick(iso: string) {
    setSelectedDay(iso);
    setFormDate(iso);
    setShowAddForm(true);
  }

  function handleEventClick(ev: CalendarEvent, e: React.MouseEvent) {
    e.stopPropagation();
    setSelectedEvent(ev);
  }

  async function handleMarkDone(id: string) {
    setMarkDoneError(null);
    try {
      await markCalendarEventDone(id);
      setSelectedEvent(null);
      load();
    } catch (err) {
      setMarkDoneError(err instanceof Error ? err.message : 'Failed to mark done');
    }
  }

  async function handleAddEvent(e: React.FormEvent) {
    e.preventDefault();
    if (!formTitle.trim() || !formDate) { setFormError('Title and date are required.'); return; }
    setFormSubmitting(true);
    setFormError(null);
    try {
      const payload: CreateCalendarEventPayload = {
        title: formTitle.trim(),
        event_date: formDate,
        event_time: formTime || undefined,
        event_type: formType || 'reminder',
        project_id: formProject || undefined,
        machine: formMachine || undefined,
        description: formDescription || undefined,
        prompt: formPrompt || undefined,
        recurrence: formRecurrence || undefined,
        created_by: 'human',
      };
      await createCalendarEvent(payload);
      setShowAddForm(false);
      setFormTitle(''); setFormDate(''); setFormTime(''); setFormType('reminder');
      setFormProject(''); setFormMachine(''); setFormDescription(''); setFormPrompt('');
      setFormRecurrence('');
      load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Failed to create event');
    } finally {
      setFormSubmitting(false);
    }
  }

  const headerActions = (
    <div className="cal-controls">
      <select
        id="cal-filter-project"
        value={filterProject}
        onChange={e => setFilterProject(e.target.value)}
      >
        <option value="">All Projects</option>
        {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
      <select
        id="cal-filter-type"
        value={filterType}
        onChange={e => setFilterType(e.target.value)}
      >
        <option value="">All Types</option>
        {Object.entries(EVENT_TYPE_EMOJI).map(([t, e]) => (
          <option key={t} value={t}>{e} {t}</option>
        ))}
      </select>
      <button
        id="cal-add-btn"
        className="btn-primary"
        type="button"
        onClick={() => { setFormDate(todayStr); setShowAddForm(true); }}
      >
        + Add Event
      </button>
    </div>
  );

  return (
    <PageShell
      title="Calendar"
      actions={headerActions}
      headerWidth="default"
      contentWidth="full"
    >
      <div className="cal-page">
        {loading && <div className="cal-loading">Loading calendar…</div>}
        {error && <div className="cal-error">Error: {error}</div>}

        {!loading && !error && (
          <>
            {/* Tabs */}
            <div className="cal-tabs" role="tablist">
              <button
                id="cal-tab-today"
                role="tab"
                aria-selected={activeTab === 'today'}
                aria-controls="cal-panel-today"
                className={`cal-tab${activeTab === 'today' ? ' active' : ''}`}
                onClick={() => setActiveTab('today')}
                type="button"
              >
                📋 Today
              </button>
              <button
                id="cal-tab-calendar"
                role="tab"
                aria-selected={activeTab === 'calendar'}
                aria-controls="cal-panel-calendar"
                className={`cal-tab${activeTab === 'calendar' ? ' active' : ''}`}
                onClick={() => setActiveTab('calendar')}
                type="button"
              >
                📅 Month
              </button>
              <button
                id="cal-tab-crons"
                role="tab"
                aria-selected={activeTab === 'crons'}
                aria-controls="cal-panel-crons"
                className={`cal-tab${activeTab === 'crons' ? ' active' : ''}`}
                onClick={() => setActiveTab('crons')}
                type="button"
              >
                ⚙️ Cron Jobs
                {crons.length > 0 && <span className="cal-badge">{crons.length}</span>}
              </button>
            </div>

            {activeTab === 'today' && todays && (
              <div id="cal-panel-today" role="tabpanel" aria-labelledby="cal-tab-today" className="cal-today">
                <h2 className="cal-today-date">{formatDate(todays.date)}</h2>
                {todays.unreadable.length > 0 && (
                  <p className="cal-error">
                    ⚠️ {todays.unreadable.length} event(s) have an unreadable stored date or time:{' '}
                    {todays.unreadable.map(ev => ev.title).join(', ')}
                  </p>
                )}
                {todaysEvents.length === 0 ? (
                  <p className="cal-today-empty">Nothing scheduled today.</p>
                ) : (
                  <ul className="cal-today-list">
                    {todaysEvents.map(ev => (
                      <li key={ev.id}>
                        <button type="button" className="cal-today-item" onClick={e => handleEventClick(ev, e)}>
                          <span className="cal-today-time">
                            {ev.event_time ? `${ev.event_time}${ev.end_time ? `–${ev.end_time}` : ''}` : 'All day'}
                          </span>
                          <span className="cal-today-title">{EVENT_TYPE_EMOJI[ev.event_type] ?? ''} {ev.title}</span>
                          <span className="cal-today-meta">
                            {[ev.event_type, ev.project_id, ev.recurrence].filter(Boolean).join(' · ')}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {activeTab === 'calendar' && (
              <div
                id="cal-panel-calendar"
                role="tabpanel"
                aria-labelledby="cal-tab-calendar"
                className="cal-layout"
              >
                {/* ── Monthly Grid ── */}
                <div className="cal-main">
                  <div className="cal-nav">
                    <button type="button" onClick={prevMonth} className="cal-nav-btn" aria-label="Previous month">‹</button>
                    <h2 className="cal-month-label">{MONTH_NAMES[viewMonth]} {viewYear}</h2>
                    <button type="button" onClick={nextMonth} className="cal-nav-btn" aria-label="Next month">›</button>
                  </div>

                  <div className="cal-grid" role="grid">
                    {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map(d => (
                      <div key={d} className="cal-weekday" role="columnheader">{d}</div>
                    ))}
                    {days.map((d, i) => {
                      const iso = isoDate(d);
                      const isCurrentMonth = d.getMonth() === viewMonth;
                      const isToday = iso === todayStr;
                      const dayEvents = eventsByDate.get(iso) ?? [];
                      return (
                        <div
                          key={i}
                          role="gridcell"
                          className={[
                            'cal-day',
                            isCurrentMonth ? '' : 'cal-day--other',
                            isToday ? 'cal-day--today' : '',
                            selectedDay === iso ? 'cal-day--selected' : '',
                          ].join(' ')}
                          onClick={() => handleDayClick(iso)}
                          tabIndex={isCurrentMonth ? 0 : -1}
                          aria-label={`${iso}${dayEvents.length ? `, ${dayEvents.length} event(s)` : ''}`}
                        >
                          <span className="cal-day-num">{d.getDate()}</span>
                          <div className="cal-day-events">
                            {dayEvents.slice(0, 3).map(ev => (
                              <button
                                key={ev.id}
                                type="button"
                                className={`cal-event-pill cal-event-pill--${ev.event_type}${ev.machine === 'MacBook' ? ' cal-event-pill--mine' : ''}`}
                                style={{ borderLeftColor: MACHINE_COLOR[ev.machine ?? ''] || 'var(--cal-default)' }}
                                onClick={e => handleEventClick(ev, e)}
                                title={`${EVENT_TYPE_EMOJI[ev.event_type] ?? ''} ${ev.title}`}
                              >
                                {EVENT_TYPE_EMOJI[ev.event_type] ?? '📌'} {ev.title}
                              </button>
                            ))}
                            {dayEvents.length > 3 && (
                              <span className="cal-more">+{dayEvents.length - 3} more</span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* ── Upcoming Sidebar ── */}
                <aside className="cal-sidebar">
                  <h3 className="cal-sidebar-title">Upcoming (14 days)</h3>
                  {upcoming.length === 0 ? (
                    <p className="cal-sidebar-empty">No events in the next 14 days.</p>
                  ) : (
                    <ul className="cal-upcoming-list">
                      {upcoming.map(ev => (
                        <li key={ev.id} className="cal-upcoming-item">
                          <button
                            type="button"
                            className="cal-upcoming-btn"
                            onClick={() => setSelectedEvent(ev)}
                          >
                            <div className="cal-upcoming-row">
                              <span className="cal-upcoming-emoji">{EVENT_TYPE_EMOJI[ev.event_type] ?? '📌'}</span>
                              <div className="cal-upcoming-info">
                                <span className="cal-upcoming-title">{ev.title}</span>
                                <span className="cal-upcoming-date">{formatDateShort(ev.event_date)}{ev.event_time ? ` · ${ev.event_time}` : ''}</span>
                                {ev.machine && (
                                  <span
                                    className="cal-machine-chip"
                                    style={{ background: MACHINE_COLOR[ev.machine] || 'var(--cal-default)' }}
                                  >
                                    {ev.machine}
                                  </span>
                                )}
                              </div>
                            </div>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </aside>
              </div>
            )}

            {activeTab === 'crons' && (
              <div
                id="cal-panel-crons"
                role="tabpanel"
                aria-labelledby="cal-tab-crons"
                className="cal-crons"
              >
                <ScheduledJobsPanel projects={projects.map(p => p.id)} />
                <h3 className="cal-sched-title">Recorded in the calendar</h3>
                <p className="cal-crons-intro">
                  All scheduled automation across your projects. Machine designation shows where the job runs.
                  Jobs without a machine set show <code>?</code>.
                </p>
                {crons.length === 0 ? (
                  <div className="cal-crons-empty">No cron jobs found. Add one via <code>pt calendar add-cron</code></div>
                ) : (
                  <table className="cal-cron-table" aria-label="Cron jobs">
                    <thead>
                      <tr>
                        <th>Project</th>
                        <th>Schedule</th>
                        <th>Machine</th>
                        <th>Description</th>
                        <th>Command</th>
                        <th>Last Run</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {crons.map(c => (
                        <tr key={c.id} className={c.is_active ? '' : 'cal-cron-inactive'}>
                          <td><span className="cal-cron-project">{c.project_id}</span></td>
                          <td><code className="cal-cron-schedule">{c.schedule}</code></td>
                          <td>
                            <span
                              className="cal-machine-chip"
                              style={{ background: MACHINE_COLOR[c.machine ?? ''] || 'var(--cal-unknown)' }}
                            >
                              {c.machine ?? '?'}
                            </span>
                          </td>
                          <td className="cal-cron-desc">{c.description ?? '—'}</td>
                          <td><code className="cal-cron-cmd">{c.command}</code></td>
                          <td className="cal-cron-last">{c.last_run ? formatDateShort(c.last_run.slice(0, 10)) : '—'}</td>
                          <td>
                            <span className={`cal-status-chip cal-status-chip--${c.is_active ? 'active' : 'inactive'}`}>
                              {c.is_active ? 'active' : 'inactive'}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {/* ── Event Detail Panel ── */}
      {selectedEvent && (
        <div
          className="cal-overlay"
          onClick={() => setSelectedEvent(null)}
          onKeyDown={e => e.key === 'Escape' && setSelectedEvent(null)}
          role="dialog"
          aria-modal
          aria-label="Event detail"
          tabIndex={-1}
        >
          <div className="cal-panel" onClick={e => e.stopPropagation()}>
            <button
              type="button"
              className="cal-panel-close"
              onClick={() => setSelectedEvent(null)}
              aria-label="Close"
            >×</button>
            <div className="cal-panel-type-badge"
              style={{ background: `color-mix(in srgb, ${MACHINE_COLOR[selectedEvent.machine ?? ''] || 'var(--cal-default)'} 20%, transparent)` }}
            >
              {EVENT_TYPE_EMOJI[selectedEvent.event_type] ?? '📌'} {selectedEvent.event_type}
            </div>
            <h2 className="cal-panel-title">{selectedEvent.title}</h2>
            <div className="cal-panel-meta">
              <span>📅 {formatDate(selectedEvent.event_date)}{selectedEvent.event_time ? ` · ${selectedEvent.event_time}` : ''}</span>
              {selectedEvent.machine && (
                <span className="cal-machine-chip"
                  style={{ background: MACHINE_COLOR[selectedEvent.machine] || 'var(--cal-default)' }}>
                  {selectedEvent.machine}
                </span>
              )}
              {selectedEvent.project_id && <span className="cal-cron-project">{selectedEvent.project_id}</span>}
              {selectedEvent.recurrence && <span className="cal-recurrence-chip">↻ {selectedEvent.recurrence}</span>}
            </div>
            {selectedEvent.description && (
              <p className="cal-panel-desc">{selectedEvent.description}</p>
            )}
            {selectedEvent.prompt && (
              <div className="cal-panel-prompt">
                <span className="cal-panel-prompt-label">🤖 Agent prompt</span>
                <pre>{selectedEvent.prompt}</pre>
              </div>
            )}
            {selectedEvent.linked_tasks && selectedEvent.linked_tasks.length > 0 && (
              <div className="cal-panel-linked">
                <h4>Linked tasks</h4>
                <ul>
                  {selectedEvent.linked_tasks.map(lt => (
                    <li key={lt.task_id}>Task #{lt.task_id} · <em>{lt.link_type}</em></li>
                  ))}
                </ul>
              </div>
            )}
            <div className="cal-panel-actions">
              {markDoneError && <p className="cal-form-error" style={{margin: 0}}>{markDoneError}</p>}
              <button
                type="button"
                className="btn-primary"
                onClick={() => handleMarkDone(selectedEvent.id)}
              >
                ✓ Mark Done
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setSelectedEvent(null)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Add Event Slide-out ── */}
      {showAddForm && (
        <div
          className="cal-overlay"
          onClick={() => setShowAddForm(false)}
          onKeyDown={e => e.key === 'Escape' && setShowAddForm(false)}
          role="dialog"
          aria-modal
          aria-label="Add event"
          tabIndex={-1}
        >
          <div className="cal-panel cal-panel--form" onClick={e => e.stopPropagation()}>
            <button type="button" className="cal-panel-close" onClick={() => setShowAddForm(false)} aria-label="Close">×</button>
            <h2 className="cal-panel-title">New Calendar Event</h2>
            {formError && <div className="cal-form-error">{formError}</div>}
            <form onSubmit={handleAddEvent} className="cal-form" id="cal-add-form">
              <label htmlFor="cf-title">Title *</label>
              <input id="cf-title" type="text" value={formTitle} onChange={e => setFormTitle(e.target.value)} placeholder="Event title…" required autoFocus />

              <label htmlFor="cf-date">Date *</label>
              <input id="cf-date" type="date" value={formDate} onChange={e => setFormDate(e.target.value)} required />

              <label htmlFor="cf-time">Time (optional)</label>
              <input id="cf-time" type="time" value={formTime} onChange={e => setFormTime(e.target.value)} />

              <label htmlFor="cf-type">Type</label>
              <select id="cf-type" value={formType} onChange={e => setFormType(e.target.value)}>
                {Object.entries(EVENT_TYPE_EMOJI).map(([t, emoji]) => (
                  <option key={t} value={t}>{emoji} {t}</option>
                ))}
              </select>

              <label htmlFor="cf-project">Project</label>
              <select id="cf-project" value={formProject} onChange={e => setFormProject(e.target.value)}>
                <option value="">None</option>
                {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>

              <label htmlFor="cf-machine">Machine</label>
              <select id="cf-machine" value={formMachine} onChange={e => setFormMachine(e.target.value)}>
                <option value="">Any</option>
                <option>MacBook</option>
                <option>OpenClaw</option>
                <option>Both</option>
                <option>web</option>
              </select>

              <label htmlFor="cf-recurrence">Recurrence</label>
              <select id="cf-recurrence" value={formRecurrence} onChange={e => setFormRecurrence(e.target.value)}>
                <option value="">None</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>

              <label htmlFor="cf-desc">Description</label>
              <textarea id="cf-desc" value={formDescription} onChange={e => setFormDescription(e.target.value)} rows={2} placeholder="Human-readable notes…" />

              <label htmlFor="cf-prompt">Agent prompt (optional)</label>
              <textarea id="cf-prompt" value={formPrompt} onChange={e => setFormPrompt(e.target.value)} rows={3} placeholder="What should the agent do when this event fires?" />

              <div className="cal-form-actions">
                <button type="submit" className="btn-primary" disabled={formSubmitting} id="cf-submit">
                  {formSubmitting ? 'Adding…' : 'Add Event'}
                </button>
                <button type="button" className="btn-secondary" onClick={() => setShowAddForm(false)}>Cancel</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </PageShell>
  );
}
