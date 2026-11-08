// Snowflake-scale calendar event ids must reach the API call as the exact string (#7826).
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { CalendarPage } from './CalendarPage';

const EVENT_ID = '98969975881101312';
const TASK_ID = '98969975881101399';

afterEach(() => {
  vi.unstubAllGlobals();
});

function isoToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

describe('CalendarPage id precision', () => {
  it('mark-done PATCHes the exact string event id and renders the linked task id intact', async () => {
    const user = userEvent.setup();
    const eventsText = `{"events":[{"id":"${EVENT_ID}","title":"Big event","event_date":"${isoToday()}","event_type":"reminder","notify_before_minutes":60,"status":"active","created_at":"x","updated_at":"x","linked_tasks":[{"id":"${TASK_ID}","task_id":"${TASK_ID}","link_type":"related"}]}],"total":1}`;
    const fetchMock = vi.fn((url: string, options?: RequestInit) => {
      let body = '{}';
      if (url.includes('/calendar/events') && !options?.method) body = eventsText;
      else if (url.includes('/calendar/today')) body = `{"date":"${isoToday()}","unreadable":[],"events":${eventsText.slice(10, eventsText.indexOf(',"total"'))}}`;
      else if (url.includes('/calendar/crons')) body = '{"cron_jobs":[],"total":0}';
      else if (url.includes('/projects')) body = '[]';
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(JSON.parse(body)),
      } as Response);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<CalendarPage />);
    const pills = await screen.findAllByRole('button', { name: /Big event/ });
    await user.click(pills[0]);
    expect(await screen.findByText(new RegExp(`Task #${TASK_ID}`))).toBeTruthy();
    await user.click(screen.getByRole('button', { name: /Mark Done/ }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([, o]) => (o as RequestInit | undefined)?.method === 'PATCH')).toBe(true)
    );
    const patch = fetchMock.mock.calls.find(([, o]) => (o as RequestInit | undefined)?.method === 'PATCH')!;
    expect(patch[0]).toBe(`/api/calendar/events/${EVENT_ID}/done`);
  });
});

describe('CalendarPage Today view', () => {
  it('opens on Today and shows each event in full, with its time, end and project', async () => {
    const title = 'Deep work: job applications, no Slack, no email until this block ends';
    const today = {
      date: '2026-11-02',
      unreadable: [{ id: '9', title: 'Old garbled event', event_date: '202６-11-02', event_type: 'reminder', notify_before_minutes: 60, status: 'active', created_at: 'x', updated_at: 'x' }],
      events: [
        { id: '1', title: 'Submit timesheet', event_date: '2026-11-02', event_type: 'deadline', notify_before_minutes: 60, status: 'active', created_at: 'x', updated_at: 'x' },
        { id: '2', title, event_date: '2026-10-01', event_time: '09:00', end_time: '11:00', recurrence: 'daily', project_id: 'project-tracker', event_type: 'reminder', notify_before_minutes: 60, status: 'active', created_at: 'x', updated_at: 'x' },
      ],
    };
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      let body: unknown = {};
      if (url.includes('/calendar/today')) body = today;
      else if (url.includes('/calendar/events')) body = { events: [], total: 0 };
      else if (url.includes('/calendar/crons')) body = { cron_jobs: [], total: 0 };
      else if (url.includes('/projects')) body = [];
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
    }));

    render(<CalendarPage />);
    const item = await screen.findByRole('button', { name: new RegExp(title.replace(/[()]/g, '.')) });
    expect(item.textContent).toContain('09:00–11:00');
    expect(item.textContent).toContain('reminder · project-tracker · daily');
    expect(screen.getByRole('button', { name: /All day.*Submit timesheet/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: /Today/ }).getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByText(/AI-first scheduling/)).toBeNull();
    expect(screen.getByText(/1 event\(s\) have an unreadable stored date or time: Old garbled event/)).toBeTruthy();
  });

  it('applies the project and type filters to Today, alone, together and cleared', async () => {
    const user = userEvent.setup();
    const ev = (id: string, title: string, event_type: string, project_id?: string) => ({
      id, title, event_date: '2026-11-02', event_type, project_id, notify_before_minutes: 60, status: 'active', created_at: 'x', updated_at: 'x',
    });
    const today = { date: '2026-11-02', unreadable: [], events: [ev('1', 'Alpha meeting', 'meeting', 'alpha'), ev('2', 'Beta deadline', 'deadline', 'beta'), ev('3', 'Alpha deadline', 'deadline', 'alpha')] };
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      let body: unknown = {};
      if (url.includes('/calendar/today')) body = today;
      else if (url.includes('/calendar/events')) body = { events: [], total: 0 };
      else if (url.includes('/calendar/crons')) body = { cron_jobs: [], total: 0 };
      else if (url.includes('/projects')) body = { projects: [{ id: 'alpha', name: 'Alpha' }, { id: 'beta', name: 'Beta' }] };
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);
    }));
    render(<CalendarPage />);
    await screen.findByRole('button', { name: /Alpha meeting/ });
    const shown = () => ['Alpha meeting', 'Beta deadline', 'Alpha deadline'].filter(t => screen.queryByRole('button', { name: new RegExp(t) }));
    await user.selectOptions(document.getElementById('cal-filter-project')!, 'alpha');
    expect(shown()).toEqual(['Alpha meeting', 'Alpha deadline']);
    await user.selectOptions(document.getElementById('cal-filter-type')!, 'deadline');
    expect(shown()).toEqual(['Alpha deadline']);
    await user.selectOptions(document.getElementById('cal-filter-project')!, '');
    expect(shown()).toEqual(['Beta deadline', 'Alpha deadline']);
    await user.selectOptions(document.getElementById('cal-filter-type')!, '');
    expect(shown()).toEqual(['Alpha meeting', 'Beta deadline', 'Alpha deadline']);
  });
});
