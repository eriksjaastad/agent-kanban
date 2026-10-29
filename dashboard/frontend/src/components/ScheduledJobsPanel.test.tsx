// The Cron Jobs tab's live view of this Mac's scheduled jobs (#8377).
import { render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ScheduledJobsPanel } from './ScheduledJobsPanel';

afterEach(() => {
  vi.unstubAllGlobals();
});

function serve(status: number, body: unknown) {
  const fetchMock = vi.fn(() =>
    Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) } as Response),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const job = (over: object) => ({
  source: 'launchd', label: 'com.t.on', project: '', settings: '{"RunAtLoad":true}', schedule: 'at login',
  path: '/a.plist', error: null, status: 'active', last_exit: 0, running: false, ...over,
});

const cells = (name: string) =>
  within(screen.getByRole('table', { name })).getAllByRole('row').slice(1)
    .map(r => within(r).getAllByRole('cell').map(c => c.firstChild?.textContent ?? ''));

describe('ScheduledJobsPanel', () => {
  it('groups jobs by project A-Z, then no project, with readable schedules and last runs', async () => {
    const fetchMock = serve(200, {
      jobs: [
        job({ label: 'x.none', last_exit: null, status: 'inactive' }),
        job({ label: 'z.ok', project: 'zeta' }),
        job({ label: 'a.fail', project: 'alpha', last_exit: 78, schedule: 'daily 07:00' }),
        job({ label: 'a.run', project: 'alpha', running: true, schedule: 'kept running' }),
        job({ label: 'a.killed', project: 'alpha', last_exit: -9 }),
        job({ label: 'a.fresh', project: 'alpha', last_exit: null }),
        job({ label: 'broken', status: 'unknown', settings: '', schedule: '', last_exit: null,
              error: 'unreadable plist: bad' }),
        job({ source: 'cron', label: './run', project: 'zeta', status: 'in crontab', settings: '0 12 * * *',
              schedule: 'daily 12:00', path: 'crontab', last_exit: null }),
      ],
      errors: ['crontab -l failed: exit 2: boom'],
    });
    render(<ScheduledJobsPanel projects={['zeta', 'beta', 'alpha', 'gamma']} />);
    await screen.findByRole('table', { name: 'Scheduled jobs: alpha' });

    const chips = within(screen.getByRole('navigation', { name: 'Projects with scheduled jobs' })).getAllByRole('link');
    expect(chips.map(c => [c.textContent, c.getAttribute('href')])).toEqual([
      ['alpha (4)', '#sched-alpha'], ['zeta (2)', '#sched-zeta'], ['No project (2)', '#sched-none'],
    ]);
    expect(screen.getAllByRole('heading', { level: 4 }).map(h => h.textContent)).toEqual(['alpha', 'zeta', 'No project']);
    // A source and a job could not be read, so no project can be called empty.
    expect(screen.queryByText(/No scheduled jobs:/)).toBeNull();
    expect(screen.getByText("Some jobs could not be read, so this can't list the projects with none.")).toBeTruthy();
    expect(cells('Scheduled jobs: alpha')).toEqual([
      ['a.fail', 'active', 'daily 07:00', 'Failed (exit 78)'],
      ['a.run', 'active', 'kept running', 'Running now'],
      ['a.killed', 'active', 'at login', 'Killed (signal 9)'],
      ['a.fresh', 'active', 'at login', 'Not run yet'],
    ]);
    expect(cells('Scheduled jobs: zeta')).toEqual([
      ['z.ok', 'active', 'at login', 'OK'],
      ['./run', 'in crontab', 'daily 12:00', '—'],
    ]);
    expect(cells('Scheduled jobs: No project')).toEqual([
      ['x.none', 'inactive', 'at login', '—'],
      ['broken', 'unknown', '—', '—'],
    ]);
    expect(screen.getByText('unreadable plist: bad')).toBeTruthy();
    expect(screen.getAllByText('Full settings')).toHaveLength(7);
    expect(screen.getByRole('alert').textContent).toBe('Could not read: crontab -l failed: exit 2: boom');
    expect(fetchMock).toHaveBeenCalledWith('/api/scheduled-jobs', expect.anything());
  });

  it('names the projects with no jobs only when every source and job was read', async () => {
    serve(200, { jobs: [job({ project: 'alpha' })], errors: [] });
    const { unmount } = render(<ScheduledJobsPanel projects={['gamma', 'alpha', 'beta']} />);
    expect(await screen.findByText('No scheduled jobs: beta, gamma')).toBeTruthy();
    unmount();

    for (const body of [{ jobs: [job({ project: 'alpha' })], errors: ['crontab -l failed: boom'] },
                        { jobs: [job({ project: 'alpha' }), job({ error: 'unreadable plist: x' })], errors: [] }]) {
      serve(200, body);
      const view = render(<ScheduledJobsPanel projects={['alpha', 'beta']} />);
      expect(await screen.findByText("Some jobs could not be read, so this can't list the projects with none.")).toBeTruthy();
      expect(screen.queryByText(/No scheduled jobs:/)).toBeNull();
      view.unmount();
    }
  });

  it('says so when the request fails, and when there are no jobs', async () => {
    serve(404, { detail: 'Not Found' });
    const { unmount } = render(<ScheduledJobsPanel projects={['alpha']} />);
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load scheduled jobs: Not Found');
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText(/No scheduled jobs:/)).toBeNull();
    unmount();

    serve(200, { jobs: [], errors: [] });
    render(<ScheduledJobsPanel />);
    expect(await screen.findByText('No scheduled jobs found.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('navigation')).toBeNull();
  });
});
