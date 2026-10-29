import { render, screen, waitFor } from '@testing-library/react';
import { ProjectsProvider } from '../hooks/ProjectsProvider';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { KanbanBoard } from './KanbanBoard';

vi.mock('../api', async (importOriginal) => ({
  // Spread the real module so isAbortError is the REAL implementation --
  // a hand-copied literal here would not notice if api.ts changed.
  ...(await importOriginal<typeof import('../api')>()),
  fetchTasks: vi.fn(),
  fetchProjects: vi.fn(),
  updateTask: vi.fn(),
  createTask: vi.fn(),
  deleteTask: vi.fn(),
}));

vi.mock('./Column', () => ({ Column: () => <div data-testid="column" /> }));
vi.mock('./Notification', () => ({ Notification: () => null }));
vi.mock('./TaskForm', () => ({ TaskForm: () => null }));
vi.mock('./TaskDetailModal', () => ({ TaskDetailModal: () => null }));
vi.mock('./ProjectFilterModal', () => ({ ProjectFilterModal: () => null }));
vi.mock('./Spinner', () => ({ Spinner: () => <div>Loading</div> }));
vi.mock('./SkeletonCard', () => ({ SkeletonCard: () => <div /> }));
vi.mock('./IdeasSection', () => ({ IdeasSection: () => null }));

import { fetchProjects, fetchTasks } from '../api';
import type { Project } from '../types';

const holoapp = (checkedOut: boolean): Project => ({
  id: 'holoapp', name: 'holoapp', path: 'virtual://holoapp', status: 'active',
  checkout: checkedOut
    ? { machine: 'mini', machine_label: 'Mac Mini', holder: 'Qwen on Mini', note: null, checked_out_at: '2026-10-16T12:00:00+00:00' }
    : null,
});

function GoTo({ to }: { to: string }) {
  const navigate = useNavigate();
  return <button type="button" onClick={() => navigate(to)}>go</button>;
}

describe('KanbanBoard', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchTasks).mockResolvedValue([]);
  });

  it('hides the add task button on blocked projects', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([
      { id: 'project-tracker', name: 'Project Tracker', path: '/tmp/project-tracker', status: 'active', can_create_cards: false, blocked_card_reason: "Cards cannot be created for project 'project-tracker'" },
    ]);

    render(
      <MemoryRouter initialEntries={['/kanban/project-tracker']}>
        <Routes>
          <Route path="/kanban/:project" element={<KanbanBoard />} />
        </Routes>
      </MemoryRouter>
    , { wrapper: ProjectsProvider });

    await waitFor(() => {
      expect(fetchProjects).toHaveBeenCalled();
    });

    expect(screen.queryByRole('button', { name: /add task/i })).not.toBeInTheDocument();
  });

  it('shows the add task button on allowed projects', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([
      { id: 'smart-invoice-workflow', name: 'Smart Invoice Workflow', path: '/tmp/smart-invoice-workflow', status: 'active', can_create_cards: true, blocked_card_reason: null },
    ]);

    render(
      <MemoryRouter initialEntries={['/kanban/smart-invoice-workflow']}>
        <Routes>
          <Route path="/kanban/:project" element={<KanbanBoard />} />
        </Routes>
      </MemoryRouter>
    , { wrapper: ProjectsProvider });

    expect(await screen.findByRole('button', { name: /add task/i })).toBeInTheDocument();
  });

  it('shows a CHECKED OUT banner on a project checked out to another machine', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([
      {
        id: 'holoapp', name: 'holoapp', path: 'virtual://holoapp', status: 'active',
        checkout: { machine: 'mini', machine_label: 'Mac Mini', holder: 'Qwen on Mini', note: null, checked_out_at: '2026-10-16T12:00:00+00:00' },
      },
    ]);

    render(
      <MemoryRouter initialEntries={['/kanban/holoapp']}>
        <Routes>
          <Route path="/kanban/:project" element={<KanbanBoard />} />
        </Routes>
      </MemoryRouter>
    , { wrapper: ProjectsProvider });

    // Not by role: dnd-kit renders its own role="status" live region.
    expect(await screen.findByText('CHECKED OUT — Mac Mini')).toBeInTheDocument();
    expect(screen.getByText(/Qwen on Mini has had it since 2026-10-16/)).toBeInTheDocument();
  });

  it('picks up a checkout made while the board is open when Refresh is clicked', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([holoapp(false)]);
    render(
      <MemoryRouter initialEntries={['/kanban/holoapp']}>
        <Routes>
          <Route path="/kanban/:project" element={<KanbanBoard />} />
        </Routes>
      </MemoryRouter>
    , { wrapper: ProjectsProvider });
    await screen.findAllByTestId('column');
    expect(screen.queryByText('CHECKED OUT — Mac Mini')).not.toBeInTheDocument();

    vi.mocked(fetchProjects).mockResolvedValue([holoapp(true)]);
    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(await screen.findByText('CHECKED OUT — Mac Mini')).toBeInTheDocument();
  });

  it('refetches checkouts when the board switches project', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([holoapp(false)]);
    render(
      <MemoryRouter initialEntries={['/kanban/other']}>
        <Routes>
          <Route path="/kanban/:project" element={<><KanbanBoard /><GoTo to="/kanban/holoapp" /></>} />
        </Routes>
      </MemoryRouter>
    , { wrapper: ProjectsProvider });
    await screen.findAllByTestId('column');

    vi.mocked(fetchProjects).mockResolvedValue([holoapp(true)]);
    await userEvent.click(screen.getByRole('button', { name: 'go' }));
    expect(await screen.findByText('CHECKED OUT — Mac Mini')).toBeInTheDocument();
  });

  describe('network', () => {
    afterEach(() => vi.unstubAllGlobals());

    it('requests nothing beyond the mocked API module', async () => {
      // The board once fetched /api/system/warnings, a route the server never
      // had, and 404'd on every load (#8093). All its data comes from ../api.
      const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 404 });
      vi.stubGlobal('fetch', fetcher);
      vi.mocked(fetchProjects).mockResolvedValue([]);

      render(
        <MemoryRouter initialEntries={['/kanban']}>
          <Routes>
            <Route path="/kanban" element={<KanbanBoard />} />
          </Routes>
        </MemoryRouter>
      , { wrapper: ProjectsProvider });

      await waitFor(() => expect(fetchTasks).toHaveBeenCalled());
      await screen.findAllByTestId('column');
      expect(fetcher).not.toHaveBeenCalled();
    });
  });
});
