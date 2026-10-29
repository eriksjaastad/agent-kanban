import { render, screen, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProjectsProvider } from '../hooks/ProjectsProvider';
import { ProjectFilterModal } from './ProjectFilterModal';

vi.mock('../api', async (importOriginal) => ({
  // Spread the real module so isAbortError is the REAL implementation --
  // a hand-copied literal here would not notice if api.ts changed.
  ...(await importOriginal<typeof import('../api')>()),
  fetchProjects: vi.fn(),
}));

import { fetchProjects } from '../api';

function modal(isOpen: boolean) {
  return (
    <ProjectsProvider>
      <MemoryRouter>
        <ProjectFilterModal isOpen={isOpen} onClose={() => {}} currentProject={undefined} />
      </MemoryRouter>
    </ProjectsProvider>
  );
}

function renderModal(isOpen: boolean) {
  return render(modal(isOpen));
}

describe('ProjectFilterModal cancellation', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    // Never settles unless its signal aborts.
    vi.mocked(fetchProjects).mockImplementation((signal?: AbortSignal) =>
      new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => {
          const err = new Error('Aborted');
          err.name = 'AbortError';
          reject(err);
        });
      }),
    );
  });

  it('keeps the loading state while a superseded request aborts and a new one is in flight', async () => {
    const { rerender } = renderModal(true);

    expect(screen.getByText('Loading projects...')).toBeTruthy();

    // Close, then reopen: the reopen reloads, which aborts the in-flight
    // request and starts a fresh one. The loading text must survive the
    // aborted request settling while there is still nothing to show.
    await act(async () => {
      rerender(modal(false));
    });
    await act(async () => {
      rerender(modal(true));
    });

    expect(fetchProjects).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('Loading projects...')).toBeTruthy();
  });
});

describe('ProjectFilterModal on the shared project list', () => {
  const PROJECTS = [
    { id: 'b', name: 'Beta', task_count: 2 },
    { id: 'a', name: 'alpha', task_count: 1 },
  ] as never;

  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('fetches once when it is already open at mount', async () => {
    vi.mocked(fetchProjects).mockResolvedValue(PROJECTS);
    renderModal(true);
    expect(await screen.findByText('alpha')).toBeTruthy();
    expect(fetchProjects).toHaveBeenCalledTimes(1);
  });

  it('sends one request when mounted open after the app loaded, as KanbanBoard mounts it', async () => {
    vi.mocked(fetchProjects).mockResolvedValue(PROJECTS);
    const { rerender } = render(<ProjectsProvider><MemoryRouter>{null}</MemoryRouter></ProjectsProvider>);
    await act(async () => {});
    expect(fetchProjects).toHaveBeenCalledTimes(1);
    await act(async () => { rerender(modal(true)); });
    expect(fetchProjects).toHaveBeenCalledTimes(2);
    // The one new request is live (the first call's signal is aborted harmlessly after it settled).
    expect(vi.mocked(fetchProjects).mock.calls[1][0]?.aborted).toBe(false);
  });

  it('lists the loaded projects sorted by name, refetches on every open including the first, and keeps the list meanwhile', async () => {
    vi.mocked(fetchProjects).mockResolvedValue(PROJECTS);
    const { rerender } = renderModal(false);
    await act(async () => {});
    expect(fetchProjects).toHaveBeenCalledTimes(1);
    // First open after the app loaded: counts may be stale, so it refetches.
    await act(async () => { rerender(modal(true)); });
    const names = screen.getAllByText(/^(alpha|Beta)$/).map((n) => n.textContent);
    expect(names).toEqual(['alpha', 'Beta']);
    expect(fetchProjects).toHaveBeenCalledTimes(2);

    // Next open: a refetch that never settles. The list stays; no loading text.
    vi.mocked(fetchProjects).mockImplementation(() => new Promise(() => {}));
    await act(async () => { rerender(modal(false)); });
    await act(async () => { rerender(modal(true)); });
    expect(fetchProjects).toHaveBeenCalledTimes(3);
    expect(screen.queryByText('Loading projects...')).toBeNull();
    expect(screen.getByText('alpha')).toBeTruthy();
  });

  it('after a failed load, a reopen shows the loading text, not the stale error, until its retry settles', async () => {
    vi.mocked(fetchProjects).mockRejectedValueOnce(new Error('projects down'));
    const { rerender } = renderModal(true);
    expect(await screen.findByText('projects down')).toBeTruthy();
    expect(screen.queryByText('Loading projects...')).toBeNull();

    // KanbanBoard unmounts the modal on close and mounts it again on open.
    await act(async () => { rerender(<ProjectsProvider><MemoryRouter>{null}</MemoryRouter></ProjectsProvider>); });
    let fail: (e: Error) => void = () => {};
    vi.mocked(fetchProjects).mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }));
    await act(async () => { rerender(modal(true)); });
    expect(fetchProjects).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Loading projects...')).toBeTruthy();
    expect(screen.queryByText('projects down')).toBeNull();

    await act(async () => { fail(new Error('still down')); });
    expect(screen.getByText('still down')).toBeTruthy();
    expect(screen.queryByText('Loading projects...')).toBeNull();
  });
});

describe('ProjectFilterModal checkouts', () => {
  it('badges a project that is checked out (#8339)', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([
      {
        id: 'holoapp', name: 'holoapp', path: 'virtual://holoapp', status: 'active',
        checkout: { machine: 'mini', machine_label: 'Mac Mini', holder: 'Qwen on Mini', note: null, checked_out_at: '2026-10-16' },
      },
    ]);
    renderModal(true);
    expect(await screen.findByText('Checked out · Mac Mini')).toBeTruthy();
  });
});
