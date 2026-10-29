import { render, screen, waitFor } from '@testing-library/react';
import { ProjectsProvider } from '../hooks/ProjectsProvider';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Task } from '../types';
import { TaskDetailModal } from './TaskDetailModal';

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  updateTask: vi.fn(),
  fetchTask: vi.fn(),
  toggleChecklistItem: vi.fn(),
  fetchProjects: vi.fn(),
  fetchAttachments: vi.fn(),
}));

import {
  NotesConflictError,
  fetchAttachments,
  fetchProjects,
  fetchTask,
  toggleChecklistItem,
} from '../api';

const BIG_ID = '98969975881101312';
const NOTES = 'intro\n- [ ] first\n- [x] second';

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: BIG_ID,
    text: 'A card',
    status: 'In Progress',
    project_id: 'project-tracker',
    priority: null,
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

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('TaskDetailModal checklist toggle (#7821)', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchProjects).mockResolvedValue([]);
    vi.mocked(fetchAttachments).mockResolvedValue([]);
  });

  const renderModal = (task = makeTask(), onNotesChanged = vi.fn()) => {
    render(
      <TaskDetailModal
        task={task}
        onClose={vi.fn()}
        onUpdate={vi.fn()}
        onNotesChanged={onNotesChanged}
      />
    , { wrapper: ProjectsProvider });
    return onNotesChanged;
  };

  it('renders checklist lines as checkboxes and other text as text', () => {
    renderModal();
    const boxes = screen.getAllByRole('checkbox');
    expect(boxes).toHaveLength(2);
    expect(boxes[0]).not.toBeChecked();
    expect(boxes[1]).toBeChecked();
    expect(screen.getByTestId('task-notes').textContent).toContain('intro');
  });

  it('ticking sends the toggle with line index and text, string id, and merges the response', async () => {
    const user = userEvent.setup();
    const newNotes = 'intro\n- [x] first\n- [x] second';
    vi.mocked(toggleChecklistItem).mockResolvedValue(
      makeTask({ notes: newNotes, updated_at: '2026-10-02T00:00:00' })
    );
    const onNotesChanged = renderModal();

    await user.click(screen.getAllByRole('checkbox')[0]);

    expect(toggleChecklistItem).toHaveBeenCalledWith(BIG_ID, 1, 'first', true, NOTES);
    await waitFor(() => expect(screen.getAllByRole('checkbox')[0]).toBeChecked());
    expect(onNotesChanged).toHaveBeenCalledWith(BIG_ID, newNotes, '2026-10-02T00:00:00');
  });

  it('disables checkboxes and Edit while a toggle is pending', async () => {
    const user = userEvent.setup();
    const pending = deferred<Task>();
    vi.mocked(toggleChecklistItem).mockReturnValue(pending.promise);
    renderModal();

    await user.click(screen.getAllByRole('checkbox')[0]);

    screen.getAllByRole('checkbox').forEach((box) => expect(box).toBeDisabled());
    expect(screen.getByRole('button', { name: 'Edit Task' })).toBeDisabled();

    pending.resolve(makeTask({ notes: 'intro\n- [x] first\n- [x] second' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit Task' })).toBeEnabled());
  });

  it('a toggle response does not reset an open edit draft', async () => {
    const user = userEvent.setup();
    const pending = deferred<Task>();
    vi.mocked(toggleChecklistItem).mockReturnValue(pending.promise);
    renderModal();

    await user.click(screen.getAllByRole('checkbox')[0]);
    pending.resolve(makeTask({ notes: 'intro\n- [x] first\n- [x] second' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit Task' })).toBeEnabled());

    await user.click(screen.getByRole('button', { name: 'Edit Task' }));
    const textarea = screen.getByPlaceholderText(/Optional notes/);
    // The draft starts from the toggled notes, not the stale prop.
    expect(textarea).toHaveValue('intro\n- [x] first\n- [x] second');
    await user.type(textarea, ' extra');
    expect(textarea).toHaveValue('intro\n- [x] first\n- [x] second extra');
  });

  it('409 shows a message and refetches the task', async () => {
    const user = userEvent.setup();
    vi.mocked(toggleChecklistItem).mockRejectedValue(new NotesConflictError('moved'));
    vi.mocked(fetchTask).mockResolvedValue(
      makeTask({ notes: '- [ ] new top\n- [ ] first\n- [x] second' })
    );
    const onNotesChanged = renderModal();

    await user.click(screen.getAllByRole('checkbox')[0]);

    expect(await screen.findByRole('status')).toHaveTextContent(/changed elsewhere/);
    expect(fetchTask).toHaveBeenCalledWith(BIG_ID);
    await waitFor(() => expect(screen.getAllByRole('checkbox')).toHaveLength(3));
    expect(onNotesChanged).toHaveBeenCalledTimes(1);
  });

  it('other errors show the error, then reload the notes the server has', async () => {
    // A 5xx can arrive after the write committed: the modal must not keep
    // its guessed checkbox state.
    const user = userEvent.setup();
    const persisted = 'intro\n- [x] first\n- [x] second';
    vi.mocked(toggleChecklistItem).mockRejectedValue(new Error('server exploded'));
    vi.mocked(fetchTask).mockResolvedValue(makeTask({ notes: persisted }));
    const onNotesChanged = renderModal();

    await user.click(screen.getAllByRole('checkbox')[0]);

    expect(await screen.findByRole('status')).toHaveTextContent('server exploded');
    expect(fetchTask).toHaveBeenCalledWith(BIG_ID);
    await waitFor(() => expect(screen.getAllByRole('checkbox')[0]).toBeChecked());
    expect(onNotesChanged).toHaveBeenCalledWith(BIG_ID, persisted, expect.any(String));
    expect(screen.getAllByRole('checkbox')[0]).toBeEnabled();
  });

  it('a failed reload says so and leaves the checkbox usable', async () => {
    const user = userEvent.setup();
    vi.mocked(toggleChecklistItem).mockRejectedValue(new Error('server exploded'));
    vi.mocked(fetchTask).mockRejectedValue(new Error('offline'));
    renderModal();

    await user.click(screen.getAllByRole('checkbox')[0]);

    expect(await screen.findByRole('status')).toHaveTextContent(/Could not reload the task \(offline\)/);
    expect(screen.getAllByRole('checkbox')[0]).toBeEnabled();
  });

  it('keeps the indentation of nested checklist items', () => {
    renderModal(makeTask({ notes: '- [ ] parent\n    - [ ] child' }));
    const labels = screen.getAllByRole('checkbox').map((box) => box.closest('label')!.textContent);
    // indent + checkbox + ' ' + text + newline
    expect(labels).toEqual([' parent\n', '     child\n']);
  });

  it('sends the notes it rendered as the base, including after a previous toggle', async () => {
    const user = userEvent.setup();
    const afterFirst = 'intro\n- [x] first\n- [x] second';
    vi.mocked(toggleChecklistItem)
      .mockResolvedValueOnce(makeTask({ notes: afterFirst }))
      .mockResolvedValueOnce(makeTask({ notes: 'intro\n- [x] first\n- [ ] second' }));
    renderModal();

    await user.click(screen.getAllByRole('checkbox')[0]);
    await waitFor(() => expect(screen.getAllByRole('checkbox')[0]).toBeChecked());
    await user.click(screen.getAllByRole('checkbox')[1]);

    expect(toggleChecklistItem).toHaveBeenNthCalledWith(1, BIG_ID, 1, 'first', true, NOTES);
    expect(toggleChecklistItem).toHaveBeenNthCalledWith(2, BIG_ID, 2, 'second', false, afterFirst);
  });
});
