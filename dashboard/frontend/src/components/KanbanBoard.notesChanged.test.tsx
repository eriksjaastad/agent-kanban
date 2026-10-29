import { render, screen, waitFor } from '@testing-library/react';
import { ProjectsProvider } from '../hooks/ProjectsProvider';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Task } from '../types';
import { KanbanBoard } from './KanbanBoard';

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  fetchTasks: vi.fn(),
  fetchProjects: vi.fn(),
}));

// The board row shows each task's notes and exposes a click to open it.
vi.mock('./Column', () => ({
  Column: ({ tasks, onTaskClick }: { tasks: Task[]; onTaskClick: (t: Task) => void }) => (
    <div>
      {tasks.map((t) => (
        <button key={t.id} onClick={() => onTaskClick(t)}>
          {`row:${t.id}:${t.notes}:${t.subtask_progress?.done ?? 'none'}`}
        </button>
      ))}
    </div>
  ),
}));
vi.mock('./TaskDetailModal', () => ({
  TaskDetailModal: ({ task, onNotesChanged }: {
    task: Task;
    onNotesChanged: (id: string, notes: string | null, updatedAt: string) => void;
  }) => (
    <button onClick={() => onNotesChanged(task.id, 'server notes', 'T2')}>fire-toggle</button>
  ),
}));
vi.mock('./Notification', () => ({ Notification: () => null }));
vi.mock('./TaskForm', () => ({ TaskForm: () => null }));
vi.mock('./ProjectFilterModal', () => ({ ProjectFilterModal: () => null }));
vi.mock('./Spinner', () => ({ Spinner: () => <div>Loading</div> }));
vi.mock('./SkeletonCard', () => ({ SkeletonCard: () => <div /> }));
vi.mock('./IdeasSection', () => ({ IdeasSection: () => null }));

import { fetchProjects, fetchTasks } from '../api';

describe('KanbanBoard checklist notes propagation (#7821)', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchProjects).mockResolvedValue([]);
  });

  it('merges only notes into the matching task and keeps enriched fields; modal stays open', async () => {
    const user = userEvent.setup();
    const mk = (id: string, notes: string) =>
      ({
        id, text: id, status: 'Backlog', project_id: 'p', notes, updated_at: 'T1',
        subtask_progress: { total: 2, done: 1, percent: 50 },
      }) as unknown as Task;
    vi.mocked(fetchTasks).mockResolvedValue([mk('98969975881101312', 'old'), mk('98969975881101313', 'other')]);

    render(
      <MemoryRouter initialEntries={['/kanban/p']}>
        <Routes>
          <Route path="/kanban/:project" element={<KanbanBoard />} />
        </Routes>
      </MemoryRouter>
    , { wrapper: ProjectsProvider });

    await user.click(await screen.findByText('row:98969975881101312:old:1'));
    await user.click(screen.getByText('fire-toggle'));

    await waitFor(() =>
      expect(screen.getByText('row:98969975881101312:server notes:1')).toBeInTheDocument()
    );
    expect(screen.getByText('row:98969975881101313:other:1')).toBeInTheDocument();
    expect(fetchTasks).toHaveBeenCalledTimes(1);
    expect(screen.getByText('fire-toggle')).toBeInTheDocument();
  });
});
