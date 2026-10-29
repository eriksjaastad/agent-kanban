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
