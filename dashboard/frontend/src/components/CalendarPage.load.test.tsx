import { StrictMode } from 'react';
import { render, screen, act, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CalendarPage } from './CalendarPage';

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  fetchCalendarEvents: vi.fn(),
  fetchCalendarCrons: vi.fn(),
  fetchCalendarToday: vi.fn(),
  fetchProjects: vi.fn(),
  markCalendarEventDone: vi.fn(),
}));

import userEvent from '@testing-library/user-event';
import { fetchCalendarCrons, fetchCalendarEvents, fetchCalendarToday, fetchProjects, markCalendarEventDone } from '../api';
import type { CalendarEvent } from '../api';

function abortable(signal?: AbortSignal) {
  return new Promise<never>((_resolve, reject) => {
    signal?.addEventListener('abort', () => {
      const err = new Error('Aborted');
      err.name = 'AbortError';
      reject(err);
    });
  });
}

describe('CalendarPage load', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchCalendarCrons).mockResolvedValue([]);
    vi.mocked(fetchCalendarToday).mockResolvedValue({ date: '2026-11-02', events: [], unreadable: [] });
    vi.mocked(fetchProjects).mockResolvedValue([{ id: 'p', name: 'Proj' }] as never);
  });

  it('shows the loading text, then the page, with projects in the filter', async () => {
    vi.mocked(fetchCalendarEvents).mockResolvedValue([]);
    render(<CalendarPage />);
    expect(screen.getByText('Loading calendar…')).toBeTruthy();
    expect(await screen.findByRole('option', { name: 'Proj' })).toBeTruthy();
    expect(screen.queryByText('Loading calendar…')).toBeNull();
  });

  it('blocks the page with the projects error when projects fail', async () => {
    vi.mocked(fetchCalendarEvents).mockResolvedValue([]);
    vi.mocked(fetchProjects).mockRejectedValue(new Error('projects down'));
    render(<CalendarPage />);
    expect(await screen.findByText('Error: projects down')).toBeTruthy();
    expect(screen.queryByText('Loading calendar…')).toBeNull();
  });

  it('shows the events error and aborts the request on unmount', async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(fetchCalendarEvents).mockImplementation((_p, s) => { signal = s; return abortable(s); });
    const { unmount } = render(<CalendarPage />);
    await act(async () => {});
    expect(screen.getByText('Loading calendar…')).toBeTruthy();
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('shows an events failure at once, without waiting for a pending projects request', async () => {
    vi.mocked(fetchCalendarEvents).mockRejectedValue(new Error('events down'));
    vi.mocked(fetchProjects).mockImplementation(() => new Promise(() => {}));
    render(<CalendarPage />);
    expect(await screen.findByText('Error: events down')).toBeTruthy();
    expect(screen.queryByText('Loading calendar…')).toBeNull();
  });

  it('loads once under StrictMode, with the projects in the filter', async () => {
    vi.mocked(fetchCalendarEvents).mockResolvedValue([]);
    render(<StrictMode><CalendarPage /></StrictMode>);
    expect(await screen.findByRole('option', { name: 'Proj' })).toBeTruthy();
    expect(screen.queryByText('Loading calendar…')).toBeNull();
    // StrictMode's dev-only remount aborts the first request and makes one more; never a loop.
    expect(vi.mocked(fetchProjects).mock.calls.length).toBeLessThanOrEqual(2);
    expect(vi.mocked(fetchProjects).mock.calls.filter(([s]) => !s?.aborted)).toHaveLength(1);
  });

  it('shows a projects failure on the reload after Mark Done, as one combined load', async () => {
    const d = new Date();
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    vi.mocked(fetchCalendarEvents).mockResolvedValue([
      { id: '1', title: 'Big event', event_date: today, event_type: 'reminder', notify_before_minutes: 60, status: 'active', created_at: 'x', updated_at: 'x', linked_tasks: [] },
    ] as never);
    vi.mocked(markCalendarEventDone).mockResolvedValue(undefined as never);
    const user = userEvent.setup();
    render(<CalendarPage />);
    await user.click(await screen.findByRole('tab', { name: /Month/ }));  // Today is the default tab
    const pills = await screen.findAllByRole('button', { name: /Big event/ });

    vi.mocked(fetchProjects).mockRejectedValueOnce(new Error('projects down'));
    await user.click(pills[0]);
    await user.click(await screen.findByRole('button', { name: /Mark Done/ }));
    expect(await screen.findByText('Error: projects down')).toBeTruthy();
  });

  it('shows the events error text', async () => {
    vi.mocked(fetchCalendarEvents).mockRejectedValue(new Error('events down'));
    render(<CalendarPage />);
    expect(await screen.findByText('Error: events down')).toBeTruthy();
  });

  it('reloads everything once the server\'s date moves past the shown one, on a timer or on focus', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(fetchCalendarEvents).mockResolvedValue([]);
      const day = (date: string) => ({ date, events: [], unreadable: [] });
      vi.mocked(fetchCalendarToday).mockResolvedValue(day('2026-11-02'));
      render(<CalendarPage />);
      await screen.findByRole('tab', { name: /Today/ });
      expect(vi.mocked(fetchCalendarEvents)).toHaveBeenCalledTimes(1);
      await act(async () => { vi.advanceTimersByTime(60_000); });  // server still on Nov 2
      expect(vi.mocked(fetchCalendarEvents)).toHaveBeenCalledTimes(1);
      vi.mocked(fetchCalendarToday).mockResolvedValue(day('2026-11-03'));
      await act(async () => { vi.advanceTimersByTime(60_000); });
      await waitFor(() => expect(vi.mocked(fetchCalendarEvents)).toHaveBeenCalledTimes(2));
      vi.mocked(fetchCalendarToday).mockResolvedValue(day('2026-11-04'));
      await act(async () => { window.dispatchEvent(new Event('focus')); });
      await waitFor(() => expect(vi.mocked(fetchCalendarEvents)).toHaveBeenCalledTimes(3));
      // A slow reload is never restarted by later ticks or focus.
      let finish: (v: CalendarEvent[]) => void = () => {};
      vi.mocked(fetchCalendarEvents).mockImplementation(() => new Promise((r) => { finish = r; }));
      vi.mocked(fetchCalendarToday).mockResolvedValue(day('2026-11-05'));
      await act(async () => { vi.advanceTimersByTime(60_000); });
      await waitFor(() => expect(vi.mocked(fetchCalendarEvents)).toHaveBeenCalledTimes(4));
      await act(async () => { window.dispatchEvent(new Event('focus')); vi.advanceTimersByTime(180_000); });
      expect(vi.mocked(fetchCalendarEvents)).toHaveBeenCalledTimes(4);
      await act(async () => { finish([]); });
      expect(await screen.findByText('Nothing scheduled today.')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

