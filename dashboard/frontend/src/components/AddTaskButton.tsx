import './AddTaskButton.css';

interface AddTaskButtonProps {
  onClick: () => void;
}

export function AddTaskButton({ onClick }: AddTaskButtonProps) {
  return (
    <button className="add-task-button" onClick={onClick}>
      <span className="add-task-button-icon">+</span>
      <span className="add-task-button-text">Add Task</span>
    </button>
  );
}
