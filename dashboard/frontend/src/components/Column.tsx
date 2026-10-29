import { useDroppable } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable';
import type { ProjectCheckout, Task, TaskStatus } from '../types';
import { TASK_STATUS_LABELS } from '../types';
import { TaskCard } from './TaskCard';
import './Column.css';

interface ColumnProps {
  status: TaskStatus;
  tasks: Task[];
  onTaskClick?: (task: Task) => void;
  /** Open checkouts by project id (#8339); a card from one shows a badge. */
  checkouts?: Record<string, ProjectCheckout>;
}

export function Column({ status, tasks, onTaskClick, checkouts }: ColumnProps) {
  const { setNodeRef, isOver } = useDroppable({
    id: status,
  });

  const taskIds = tasks.map(task => task.id);

  return (
    <div
      ref={setNodeRef}
      className={`column ${isOver ? 'drag-over' : ''}`}
      data-status={status}
    >
      <div className="column-header">
        <h3 className="column-title">{TASK_STATUS_LABELS[status]}</h3>
        <span className="column-count">{tasks.length}</span>
      </div>
      <SortableContext items={taskIds} strategy={verticalListSortingStrategy}>
        <div className="column-content">
          {tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              onClick={onTaskClick ? () => onTaskClick(task) : undefined}
              checkout={checkouts?.[task.project_id]}
            />
          ))}
        </div>
      </SortableContext>
    </div>
  );
}
