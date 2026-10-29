import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProjectsProvider } from '../hooks/ProjectsProvider';
import type { Task } from '../types';
import { TaskDetailModal } from './TaskDetailModal';

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  updateTask: vi.fn(),
  fetchTask: vi.fn(),
  fetchProjects: vi.fn(),
  fetchAttachments: vi.fn(),
}));

import { NotesConflictError, fetchAttachments, fetchProjects, fetchTask, updateTask } from '../api';

const NOTES = 'intro\n- [ ] first';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: '98969975881101312',
    text: 'Fish &amp; chips',
    status: 'In Progress',
    project_id: 'project-tracker',
    priority: 'High',
    task_type: 'manual',
    created_at: '2026-10-01T00:00:00',
    updated_at: '2026-10-01T00:00:00',
    completed_at: null,
    prompt: null,
    title: null,
    notes: NOTES,
    commit_sha: null,
    category: null,
    review_comment: null,
    parent_id: null,
    blocked_by: null,
    sequence_order: null,
    ...overrides,
  } as Task;
}

function renderModal(task = makeTask(), onUpdate = vi.fn()) {
  render(
    <ProjectsProvider>
      <TaskDetailModal task={task} onClose={vi.fn()} onUpdate={onUpdate} onDelete={vi.fn()} onNotesChanged={vi.fn()} />
    </ProjectsProvider>,
  );
  return { onUpdate };
}

describe('TaskDetailModal checkout', () => {
  it("badges the project when it is checked out (#8339)", async () => {
    vi.resetAllMocks();
    vi.mocked(fetchAttachments).mockResolvedValue([]);
    vi.mocked(fetchProjects).mockResolvedValue([
      {
        id: 'project-tracker', name: 'Project Tracker', path: '/p', status: 'active',
        checkout: { machine: 'mini', machine_label: 'Mac Mini', holder: 'Qwen on Mini', note: null, checked_out_at: '2026-10-16' },
      },
    ]);
    renderModal();
    expect(await screen.findByText('Checked out · Mac Mini')).toBeInTheDocument();
  });
});

describe('TaskDetailModal edit Save', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchProjects).mockResolvedValue([]);
    vi.mocked(fetchAttachments).mockResolvedValue([]);
  });

  it('sends only the fields the user changed', async () => {
    const user = userEvent.setup();
    vi.mocked(updateTask).mockResolvedValue(makeTask({ title: 'Short' }));
    const { onUpdate } = renderModal();

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    await user.type(screen.getByPlaceholderText('Optional short title...'), 'Short');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    // The untouched text is not re-sent (each write would escape it again),
    // and neither are notes, status or priority an agent may have changed.
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('98969975881101312', { title: 'Short' }));
    expect(onUpdate).toHaveBeenCalled();
  });

  it('sends changed notes with the notes the edit started from', async () => {
    const user = userEvent.setup();
    vi.mocked(updateTask).mockResolvedValue(makeTask());
    renderModal();

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    await user.type(screen.getByPlaceholderText('Optional notes, context, or comments...'), '\nsecond');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('98969975881101312', {
      notes: `${NOTES}\nsecond`,
      base_notes: NOTES,
    }));
  });

  it('does not send notes whose only difference is edge whitespace', async () => {
    const user = userEvent.setup();
    vi.mocked(updateTask).mockResolvedValue(makeTask());
    renderModal(makeTask({ notes: `${NOTES}\n` }));

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    await user.type(screen.getByPlaceholderText('Optional short title...'), 'Short');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    // Sending the trimmed notes would 409 this title-only edit whenever an
    // agent had touched the notes meanwhile.
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('98969975881101312', { title: 'Short' }));
  });

  it('makes no request when nothing changed', async () => {
    const user = userEvent.setup();
    renderModal();

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    expect(updateTask).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Edit Task' })).toBeInTheDocument();
  });

  it('keeps the draft on a notes conflict and saves against the latest notes next time', async () => {
    const user = userEvent.setup();
    const theirs = `${NOTES}\n- [x] done elsewhere`;
    vi.mocked(updateTask)
      .mockRejectedValueOnce(new NotesConflictError('changed'))
      .mockResolvedValueOnce(makeTask());
    vi.mocked(fetchTask).mockResolvedValue(makeTask({ notes: theirs, updated_at: '2026-10-02T00:00:00' }));
    renderModal();

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    const notes = screen.getByPlaceholderText('Optional notes, context, or comments...');
    await user.type(notes, '\nmine');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Save again to replace the current notes with your version');
    expect(notes).toHaveValue(`${NOTES}\nmine`);

    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(updateTask).toHaveBeenLastCalledWith('98969975881101312', {
      notes: `${NOTES}\nmine`,
      base_notes: theirs,
    }));
  });

  it('keeps the draft when the reload after a conflict fails, and the next Save reloads again', async () => {
    const user = userEvent.setup();
    const theirs = `${NOTES}\n- [x] done elsewhere`;
    vi.mocked(updateTask).mockRejectedValue(new NotesConflictError('changed'));
    vi.mocked(fetchTask).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(makeTask({ notes: theirs }));
    renderModal();

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    const notes = screen.getByPlaceholderText('Optional notes, context, or comments...');
    await user.type(notes, '\nmine');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('loading the current notes failed (offline). Your draft is kept: Save again to retry.');
    expect(notes).toHaveValue(`${NOTES}\nmine`);

    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    expect(await screen.findByText(/Save again to replace the current notes/)).toBeInTheDocument();
    expect(notes).toHaveValue(`${NOTES}\nmine`);
  });

  it('on a conflict with notes already equal to the draft, does not rewrite them', async () => {
    // A Save whose answer was lost: the server already holds this draft's notes.
    const user = userEvent.setup();
    vi.mocked(updateTask).mockRejectedValueOnce(new NotesConflictError('changed'));
    vi.mocked(fetchTask).mockResolvedValue(makeTask({ notes: `${NOTES}\nmine` }));
    renderModal();

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    await user.type(screen.getByPlaceholderText('Optional notes, context, or comments...'), '\nmine');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('The saved notes already match your version');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));
    expect(updateTask).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Edit Task' })).toBeInTheDocument();
  });

  it('shows a proposal\'s type read-only, so an edit cannot turn it into a normal card', async () => {
    const user = userEvent.setup();
    vi.mocked(updateTask).mockResolvedValue(makeTask({ task_type: 'proposal', title: 'Short' }));
    renderModal(makeTask({ task_type: 'proposal' }));

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.getByText('Proposal')).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText('Optional short title...'), 'Short');
    await user.click(screen.getByRole('button', { name: 'Save Changes' }));

    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('98969975881101312', { title: 'Short' }));
  });
});
