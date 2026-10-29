import { useMemo } from 'react';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import type { ProjectCheckout, Task } from '../types';
import { TASK_TYPE_LABELS } from '../types';
import { CheckoutBadge } from './CheckoutBadge';
import './TaskCard.css';

interface TaskCardProps {
  task: Task;
  onClick?: () => void;
  /** Set when the card's project is checked out to a machine (#8339). */
  checkout?: ProjectCheckout | null;
}

export function TaskCard({ task, onClick, checkout }: TaskCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: task.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
  };

  // Truncate for display. No escaping happens here on purpose: this text is
  // rendered as a JSX child and as a title attribute, both of which React
  // escapes. An escapeHtml() here would either be a no-op (it was) or would
  // double-escape (#6953).
  const truncatedText = useMemo(
    () => (task.text.length > 100 ? task.text.substring(0, 100) + '...' : task.text),
    [task.text]
  );

  const priorityColors: Record<string, string> = {
    Critical: 'var(--critical)',     // #dc2626 - dark alarm red
    High: 'var(--danger)',           // #ff6b6b - red
    Medium: 'var(--success)',        // #51cf66 - green
    Low: 'var(--accent)',            // #4f9dff - blue
  };

  const priorityColor = task.priority ? priorityColors[task.priority] : undefined;
  const displayId = task.display_id ?? task.id;
  const parentDisplayId = task.parent_display_id ?? task.parent_id;

  const handleClick = (e: React.MouseEvent) => {
    // Don't trigger click when dragging
    if (!isDragging && onClick) {
      e.stopPropagation();
      onClick();
    }
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`task-card ${isDragging ? 'dragging' : ''} ${onClick ? 'clickable' : ''}`}
      {...attributes}
      {...listeners}
      onClick={handleClick}
    >
      {priorityColor && (
        <div
          className="task-card-priority-indicator"
          style={{ backgroundColor: priorityColor }}
        />
      )}
      <div className="task-card-content">
        <div className="task-card-id">#{displayId}</div>
        <p className="task-card-text" title={task.title ? task.text : undefined}>
          {task.title || truncatedText}
        </p>
        <div className="task-card-meta">
          <span className="task-card-project">{task.project_id}</span>
          {checkout && <CheckoutBadge projectId={task.project_id} checkout={checkout} />}
          {task.category && (
            <span className="task-card-category" title="Category">
              {task.category}
            </span>
          )}
          {task.task_type === 'agent' && (
            <span className="task-card-type-badge" title="Agent task">
              {TASK_TYPE_LABELS[task.task_type]}
            </span>
          )}
          {/* Subtask progress badge (Task #4645) */}
          {task.subtask_progress && (
            <span className="task-card-subtask-badge" title={`${task.subtask_progress.done} of ${task.subtask_progress.total} subtasks complete`}>
              📋 {task.subtask_progress.done}/{task.subtask_progress.total}
            </span>
          )}
          {/* Blocked indicator (Task #4579).
              A card blocked by a reference that resolves to nothing is not the
              same problem as one waiting on real work — the first is a data
              bug you fix by correcting the reference, the second you fix by
              doing the task. They looked identical until #6924. */}
          {task.is_blocked && (
            (task.unresolved_blocking_ids?.length ?? 0) > 0 ? (
              <span
                className="task-card-blocked-badge task-card-blocked-badge-broken"
                title={
                  `Blocked by ${task.unresolved_blocking_ids!.length} reference(s) ` +
                  `that no longer exist: ${task.unresolved_blocking_ids!.join(', ')}. ` +
                  `Fix the blocked_by value — no task will ever clear this.`
                }
              >
                🔗 Broken blocker
              </span>
            ) : (
              <span className="task-card-blocked-badge" title="Blocked by incomplete tasks">
                🔒 Blocked
              </span>
            )
          )}
          {/* Parent indicator (Task #4645) */}
          {parentDisplayId && (
            <span className="task-card-parent-badge" title={`Subtask of #${parentDisplayId}`}>
              ↳ #{parentDisplayId}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
