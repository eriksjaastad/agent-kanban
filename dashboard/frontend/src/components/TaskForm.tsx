import { useState, useEffect } from 'react';
import type { TaskStatus, TaskPriority, Project, Task, TaskType } from '../types';
import { KANBAN_STATUSES, TASK_PRIORITIES, TASK_STATUS_LABELS, TASK_TYPE_LABELS } from '../types';
import { fetchProjects, fetchTaskPolicy, fetchTasks, isAbortError } from '../api';
import { Spinner } from './Spinner';
import { ACCEPTANCE_CRITERIA_HINT, ACCEPTANCE_CRITERIA_TEMPLATE, hasAcceptanceCriteria } from '../utils/checklist';
import './TaskForm.css';

interface TaskFormProps {
  onSubmit: (data: {
    text: string;
    projectId: string;
    status: TaskStatus;
    priority: TaskPriority | null;
    taskType: TaskType;
    notes: string;
    parentId?: string | null;
    blockedBy?: string[] | null;
  }) => Promise<void>;
  onCancel: () => void;
  initialProjectId?: string;
  initialStatus?: TaskStatus;
}

export function TaskForm({
  onSubmit,
  onCancel,
  initialProjectId,
  initialStatus = 'Backlog',
}: TaskFormProps) {
  const [text, setText] = useState('');
  const [notes, setNotes] = useState(ACCEPTANCE_CRITERIA_TEMPLATE);
  const [projectId, setProjectId] = useState(initialProjectId || '');
  const [status, setStatus] = useState<TaskStatus>(initialStatus);
  const [priority, setPriority] = useState<TaskPriority | null>(null);
  const [taskType, setTaskType] = useState<TaskType>('manual');
  const [parentId, setParentId] = useState<string | null>(null);
  const [blockedByInput, setBlockedByInput] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [blockedProjectReasons, setBlockedProjectReasons] = useState<Record<string, string | null>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const eligibleProjects = [...projects]
    .filter((project) => project.can_create_cards !== false)
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));

  useEffect(() => {
    const controller = new AbortController();
    const loadData = async () => {
      try {
        const [projectData, taskData, taskPolicy] = await Promise.all([
          fetchProjects(controller.signal),
          fetchTasks(undefined, undefined, controller.signal),
          fetchTaskPolicy(controller.signal),
        ]);
        setProjects(projectData);
        setTasks(taskData);
        setBlockedProjectReasons(taskPolicy.blocked_project_reasons || {});
        const allowedProjects = projectData.filter((project) => project.can_create_cards !== false);

        if (initialProjectId) {
          setProjectId(initialProjectId);
        } else if (allowedProjects.length > 0) {
          setProjectId(allowedProjects[0].id);
        } else {
          setProjectId('');
        }
      } catch (err) {
        if (isAbortError(err)) {
          return;
        }
        console.error('Failed to load data:', err);
        setError('Failed to load projects and tasks');
      }
    };

    loadData();
    return () => controller.abort();
  }, [initialProjectId]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!text.trim()) {
      setError('Task text is required');
      return;
    }

    if (!projectId) {
      setError('Please select a project');
      return;
    }

    const selectedProject = projects.find((project) => project.id === projectId);
    const blockedReason =
      selectedProject?.can_create_cards === false
        ? selectedProject.blocked_card_reason
        : blockedProjectReasons[projectId];
    if (blockedReason) {
      setError(blockedReason || `Cards cannot be created for project '${projectId}'`);
      return;
    }

    if (!hasAcceptanceCriteria(notes)) {
      setError(ACCEPTANCE_CRITERIA_HINT);
      return;
    }

    // Parse blocked_by: each entry is a card number (#6100 or 6100) or a
    // task id, and must name a loaded task. The server stores ids unchecked,
    // so an entry that resolves to nothing is refused here rather than
    // dropped or sent as a dangling id. Ids stay strings: they can exceed
    // Number.MAX_SAFE_INTEGER (#7824); only the small display_id is compared
    // as a number.
    let blockedBy: string[] | null = null;
    if (blockedByInput.trim()) {
      const entries = blockedByInput.split(',').map((entry) => entry.trim());
      if (entries.includes('')) {
        setError('Blocked By has an empty entry: remove the extra comma.');
        return;
      }
      const unknown: string[] = [];
      const ids = entries.map((entry) => {
        const rawId = entry.replace(/^#/, '');
        const matchedTask = /^\d+$/.test(rawId)
          ? tasks.find((task) => task.display_id === Number(rawId) || task.id === rawId)
          : undefined;
        if (!matchedTask) unknown.push(entry);
        return matchedTask?.id ?? '';
      });
      blockedBy = [...new Set(ids)];
      if (unknown.length > 0) {
        setError(`Blocked By: ${unknown.join(', ')} doesn't match any task the board has loaded. Use the number shown on the card.`);
        return;
      }
    }

    setLoading(true);
    try {
      await onSubmit({
        text: text.trim(),
        projectId,
        status,
        priority,
        taskType,
        notes: notes.trim(),
        parentId,
        blockedBy,
      });
      // Reset form on success
      setText('');
      setNotes(ACCEPTANCE_CRITERIA_TEMPLATE);
      setPriority(null);
      setTaskType('manual');
      setParentId(null);
      setBlockedByInput('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create task');
    } finally {
      setLoading(false);
    }
  };

  return (
    <form className="task-form" onSubmit={handleSubmit}>
      <div className="task-form-header">
        <h2>Create New Task</h2>
        <button
          type="button"
          className="task-form-close"
          onClick={onCancel}
          aria-label="Close"
        >
          &times;
        </button>
      </div>

      {error && (
        <div className="task-form-error" role="alert">
          {error}
        </div>
      )}

      <div className="task-form-field">
        <label htmlFor="task-text" className="task-form-label">
          Task Description <span className="required">*</span>
        </label>
        <textarea
          id="task-text"
          className="task-form-textarea"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Enter task description..."
          rows={4}
          required
          disabled={loading}
        />
        <div className="task-form-char-count">
          {text.length} characters
        </div>
      </div>

      <div className="task-form-field">
        <label htmlFor="task-acceptance-criteria" className="task-form-label">
          Acceptance Criteria <span className="required">*</span>
        </label>
        <textarea
          id="task-acceptance-criteria"
          className="task-form-textarea"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="- [ ] How will completion be verified?"
          rows={3}
          required
          disabled={loading}
        />
        <div className="task-form-hint">
          At least one <code>- [ ] &lt;how completion is verified&gt;</code> checklist
          line is required. A heading alone doesn't count. For a proposal, state the
          decision/approval criterion instead, e.g. "- [ ] Approve if X; reject if Y".
        </div>
      </div>

      <div className="task-form-row">
        <div className="task-form-field">
          <label htmlFor="task-project" className="task-form-label">
            Project <span className="required">*</span>
          </label>
          <select
            id="task-project"
            className="task-form-select"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            required
            disabled={loading}
          >
            <option value="">Select a project...</option>
            {eligibleProjects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name || project.id}
              </option>
            ))}
          </select>
          {eligibleProjects.length === 0 && (
            <div className="task-form-char-count">No card-enabled projects are available.</div>
          )}
        </div>

        <div className="task-form-field">
          <label htmlFor="task-status" className="task-form-label">
            Status
          </label>
          <select
            id="task-status"
            className="task-form-select"
            value={status}
            onChange={(e) => setStatus(e.target.value as TaskStatus)}
            disabled={loading}
          >
            {KANBAN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {TASK_STATUS_LABELS[s]}
              </option>
            ))}
          </select>
        </div>

        <div className="task-form-field">
          <label htmlFor="task-type" className="task-form-label">
            Task Type
          </label>
          <label className="task-type-toggle">
            <input
              id="task-type"
              type="checkbox"
              checked={taskType === 'agent'}
              onChange={(e) => setTaskType(e.target.checked ? 'agent' : 'manual')}
              disabled={loading}
            />
            <span className="task-type-slider" aria-hidden="true" />
            <span className="task-type-text">
              {TASK_TYPE_LABELS[taskType]}
            </span>
          </label>
        </div>
      </div>

      <div className="task-form-row">
        <div className="task-form-field">
          <label htmlFor="task-priority" className="task-form-label">
            Priority
          </label>
          <select
            id="task-priority"
            className="task-form-select"
            value={priority || ''}
            onChange={(e) =>
              setPriority(e.target.value ? (e.target.value as TaskPriority) : null)
            }
            disabled={loading}
          >
            <option value="">None</option>
            {TASK_PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>

        <div className="task-form-field">
          <label htmlFor="task-parent" className="task-form-label">
            Parent Task (optional)
          </label>
          <select
            id="task-parent"
            className="task-form-select"
            value={parentId || ''}
            onChange={(e) => setParentId(e.target.value || null)}
            disabled={loading}
          >
            <option value="">None (top-level task)</option>
            {tasks.filter(t => t.parent_id === null && t.project_id === projectId).map((task) => (
              <option key={task.id} value={task.id}>
                #{task.display_id ?? task.id} - {task.text.substring(0, 40)}{task.text.length > 40 ? '...' : ''}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="task-form-field">
        <label htmlFor="task-blocked-by" className="task-form-label">
          Blocked By (optional)
        </label>
        <input
          id="task-blocked-by"
          type="text"
          className="task-form-select"
          value={blockedByInput}
          onChange={(e) => setBlockedByInput(e.target.value)}
          placeholder="Comma-separated task IDs (e.g., 4645, 4646)"
          disabled={loading}
        />
        <div className="task-form-hint">
          Enter task IDs this task depends on, separated by commas
        </div>
      </div>

      <div className="task-form-actions">
        <button
          type="button"
          className="task-form-button task-form-button-cancel"
          onClick={onCancel}
          disabled={loading}
        >
          Cancel
        </button>
        <button
          type="submit"
          className="task-form-button task-form-button-submit"
          disabled={loading || !text.trim() || !projectId}
        >
          {loading ? (
            <>
              <Spinner size="small" />
              <span>Creating...</span>
            </>
          ) : (
            'Create Task'
          )}
        </button>
      </div>
    </form>
  );
}
