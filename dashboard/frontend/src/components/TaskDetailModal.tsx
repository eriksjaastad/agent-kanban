import { useState, useEffect, useRef, useCallback } from 'react';
import type { Task, TaskStatus, TaskPriority, TaskType, Attachment } from '../types';
import { KANBAN_STATUSES, TASK_PRIORITIES, TASK_STATUS_LABELS, TASK_TYPE_LABELS } from '../types';
import { updateTask, fetchTask, toggleChecklistItem, NotesConflictError, fetchAttachments, uploadAttachment, deleteAttachment, isAbortError } from '../api';
import { useProjects } from '../hooks/useProjects';
import { CheckoutBadge } from './CheckoutBadge';
import { parseNotesLines } from '../utils/checklist';
import './TaskDetailModal.css';

interface TaskDetailModalProps {
  task: Task | null;
  onClose: () => void;
  onUpdate: () => void;
  onDelete?: (taskId: string) => Promise<void>;
  /** A checklist toggle landed: merge only these fields into the board's copy. */
  onNotesChanged?: (taskId: string, notes: string | null, updatedAt: string) => void;
}

export function TaskDetailModal({
  task,
  onClose,
  onUpdate,
  onDelete,
  onNotesChanged,
}: TaskDetailModalProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editedText, setEditedText] = useState('');
  const [editedTitle, setEditedTitle] = useState('');
  const [editedNotes, setEditedNotes] = useState('');
  const [editedReviewComment, setEditedReviewComment] = useState('');
  const [editedCommitSha, setEditedCommitSha] = useState('');
  const [editedCategory, setEditedCategory] = useState('');
  const [editedStatus, setEditedStatus] = useState<TaskStatus>('Backlog');
  const [editedPriority, setEditedPriority] = useState<TaskPriority | null>(null);
  const [editedTaskType, setEditedTaskType] = useState<TaskType>('manual');
  // The stored notes the edit started from: Save sends them as base_notes, so
  // a change made elsewhere meanwhile is a conflict, not a silent overwrite.
  const [editBaseNotes, setEditBaseNotes] = useState<string | null>(null);
  const { projects } = useProjects();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Checklist toggles (#7821). Notes returned by the server are held here
  // (keyed by task id) instead of being pushed back through `task`, so a
  // toggle response never re-runs the effect that resets the edit draft.
  const [notesOverride, setNotesOverride] = useState<{ taskId: string; notes: string | null } | null>(null);
  const [togglePending, setTogglePending] = useState(false);
  const [toggleMessage, setToggleMessage] = useState<string | null>(null);
  const currentTaskIdRef = useRef<string | null>(null);
  currentTaskIdRef.current = task ? task.id : null;

  // Attachments state (#5216)
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFiles = useCallback(async (files: File[]) => {
    if (!task || !files.length) return;
    setUploading(true);
    try {
      for (const f of files) {
        await uploadAttachment(task.id, f);
      }
      const updated = await fetchAttachments(task.id);
      setAttachments(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  }, [task]);

  const handleFileDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    handleFiles(Array.from(e.dataTransfer.files));
  }, [handleFiles]);

  useEffect(() => {
    if (task) {
      setEditedText(task.text);
      setEditedTitle(task.title || '');
      setEditedNotes(task.notes || '');
      setEditedReviewComment(task.review_comment || '');
      setEditedCommitSha(task.commit_sha || '');
      setEditedCategory(task.category || '');
      setEditedStatus(task.status);
      setEditedPriority(task.priority);
      setEditedTaskType(task.task_type || 'manual');
      setIsEditing(false);
      setError(null);
      // Load attachments for this task
      const controller = new AbortController();
      fetchAttachments(task.id, controller.signal)
        .then(setAttachments)
        .catch((err) => {
          if (isAbortError(err)) {
            return;
          }
          setAttachments([]);
        });
      return () => controller.abort();
    }
  }, [task]);

  if (!task) {
    return null;
  }

  const currentNotes = notesOverride && notesOverride.taskId === task.id
    ? notesOverride.notes
    : task.notes;

  const applyServerNotes = (taskId: string, notes: string | null, updatedAt: string) => {
    if (currentTaskIdRef.current === taskId) {
      setNotesOverride({ taskId, notes });
    }
    onNotesChanged?.(taskId, notes, updatedAt);
  };

  const handleChecklistToggle = async (lineIndex: number, text: string, checked: boolean) => {
    if (togglePending) return;
    const taskId = task.id;
    const baseNotes = currentNotes ?? null;
    setTogglePending(true);
    setToggleMessage(null);
    try {
      const updated = await toggleChecklistItem(taskId, lineIndex, text, checked, baseNotes);
      applyServerNotes(taskId, updated.notes ?? null, updated.updated_at);
    } catch (err) {
      // Any failure leaves the stored notes unknown to us: a 409 means they
      // changed elsewhere, and a 5xx or network error may come after the
      // write committed. Either way, show what the server has now rather
      // than the checkbox state we guessed.
      if (currentTaskIdRef.current === taskId) {
        setToggleMessage(
          err instanceof NotesConflictError
            ? 'That item changed elsewhere; showing the latest notes.'
            : `${err instanceof Error ? err.message : 'Failed to toggle checklist item'}; showing the latest notes.`
        );
      }
      try {
        const fresh = await fetchTask(taskId);
        applyServerNotes(taskId, fresh.notes ?? null, fresh.updated_at);
      } catch (refetchErr) {
        if (currentTaskIdRef.current === taskId) {
          setToggleMessage(
            `Could not reload the task (${
              refetchErr instanceof Error ? refetchErr.message : 'unknown error'
            }); close and reopen it before ticking again.`
          );
        }
      }
    } finally {
      setTogglePending(false);
    }
  };

  const startEditing = () => {
    // The draft starts from the freshest notes (including toggles), not the
    // possibly stale `task` prop.
    setEditedNotes(currentNotes || '');
    setEditBaseNotes(currentNotes ?? null);
    setIsEditing(true);
  };

  const project = projects?.find((p) => p.id === task.project_id);
  const displayId = task.display_id ?? task.id;
  const parentDisplayId = task.parent_display_id ?? task.parent_id;

  const handleSave = async () => {
    if (!editedText.trim()) {
      setError('Task text cannot be empty');
      return;
    }


    // Send only what the user changed: an untouched field keeps whatever the
    // server has now, including changes an agent made while this edit was open.
    // Both sides are trimmed alike, so stored edge whitespace (common in notes)
    // never counts as an edit.
    const optional = (value: string | null | undefined) => value?.trim() || null;
    const changes: Parameters<typeof updateTask>[1] = {};
    if (editedText.trim() !== task.text.trim()) changes.text = editedText.trim();
    if (optional(editedTitle) !== optional(task.title)) changes.title = optional(editedTitle);
    if (optional(editedReviewComment) !== optional(task.review_comment)) changes.review_comment = optional(editedReviewComment);
    if (optional(editedCommitSha) !== optional(task.commit_sha)) changes.commit_sha = optional(editedCommitSha);
    if (optional(editedCategory) !== optional(task.category)) changes.category = optional(editedCategory);
    if (editedStatus !== task.status) changes.status = editedStatus;
    if (editedPriority !== task.priority) changes.priority = editedPriority;
    if (editedTaskType !== (task.task_type || 'manual')) changes.task_type = editedTaskType;
    if (optional(editedNotes) !== optional(editBaseNotes)) {
      changes.notes = optional(editedNotes);
      changes.base_notes = editBaseNotes;
    }
    if (Object.keys(changes).length === 0) {
      setIsEditing(false);
      return;
    }

    setLoading(true);
    setError(null);

    const taskId = task.id;
    try {
      await updateTask(taskId, changes);
      setIsEditing(false);
      onUpdate();
    } catch (err) {
      if (!(err instanceof NotesConflictError)) {
        setError(err instanceof Error ? err.message : 'Failed to update task');
        return;
      }
      // Keep the draft. The current notes become the base, so the user decides:
      // save again to replace them, or cancel to keep them. A failed reload
      // leaves the old base, so the next Save conflicts and reloads again.
      const stale = 'The notes no longer match what this edit started from, so this Save wrote nothing';
      try {
        const fresh = await fetchTask(taskId);
        applyServerNotes(taskId, fresh.notes ?? null, fresh.updated_at);
        if (currentTaskIdRef.current === taskId) {
          setEditBaseNotes(fresh.notes ?? null);
          // An earlier Save can land with its answer lost, so the "other"
          // writer may have been this draft: then its notes need no write.
          setError(optional(fresh.notes) === optional(editedNotes)
            ? 'The saved notes already match your version, so they need no change. Save again to apply any other changes, or Cancel.'
            : `${stale}. Save again to replace the current notes with your version, or Cancel to keep them.`);
        }
      } catch (refetchErr) {
        if (currentTaskIdRef.current === taskId) {
          setError(`${stale}, and loading the current notes failed (${
            refetchErr instanceof Error ? refetchErr.message : 'unknown error'
          }). Your draft is kept: Save again to retry.`);
        }
      }
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    setEditedText(task.text);
    setEditedTitle(task.title || '');
    setEditedNotes(currentNotes || '');
    setEditedReviewComment(task.review_comment || '');
    setEditedCommitSha(task.commit_sha || '');
    setEditedCategory(task.category || '');
    setEditedStatus(task.status);
    setEditedPriority(task.priority);
    setEditedTaskType(task.task_type || 'manual');
    setIsEditing(false);
    setError(null);
  };

  const handleDelete = async () => {
    if (!onDelete) return;

    const confirmed = window.confirm(
      'Are you sure you want to delete this task? This action cannot be undone.'
    );

    if (!confirmed) return;

    setDeleting(true);
    setError(null);

    try {
      await onDelete(task.id);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete task');
    } finally {
      setDeleting(false);
    }
  };

  const priorityColors: Record<string, string> = {
    Critical: 'var(--critical)',
    High: 'var(--danger)',
    Medium: 'var(--success)',
    Low: 'var(--accent)',
  };

  const formatDate = (dateString: string) => {
    try {
      return new Date(dateString).toLocaleString();
    } catch {
      return dateString;
    }
  };

  return (
    <div className="task-detail-modal-overlay" onClick={onClose}>
      <div
        className="task-detail-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="task-detail-modal-header">
          <div className="task-detail-header-left">
            <h2>Task Details</h2>
            <button
              className="task-id-copy-button"
              onClick={() => {
                navigator.clipboard.writeText(`#${displayId}`);
                // Could show a toast notification here
              }}
              title="Copy task ID"
            >
              #{displayId}
            </button>
          </div>
          <button
            className="task-detail-modal-close"
            onClick={onClose}
            aria-label="Close modal"
          >
            ×
          </button>
        </div>

        {error && (
          <div className="task-detail-modal-error" role="alert">
            {error}
          </div>
        )}

        <div className="task-detail-modal-content">
          {isEditing ? (
            <>
              <div className="task-detail-field">
                <label className="task-detail-label">
                  Title (short name for card)
                </label>
                <input
                  type="text"
                  className="task-detail-select"
                  value={editedTitle}
                  onChange={(e) => setEditedTitle(e.target.value)}
                  placeholder="Optional short title..."
                  maxLength={100}
                  disabled={loading}
                />
              </div>

              <div className="task-detail-field">
                <label className="task-detail-label">
                  Task Description <span className="required">*</span>
                </label>
                <textarea
                  className="task-detail-textarea"
                  value={editedText}
                  onChange={(e) => setEditedText(e.target.value)}
                  rows={6}
                  disabled={loading}
                />
                <div className="task-detail-char-count">
                  {editedText.length} characters
                </div>
              </div>

              <div className="task-detail-field">
                <label className="task-detail-label">
                  Notes (internal)
                </label>
                <textarea
                  className="task-detail-textarea"
                  value={editedNotes}
                  onChange={(e) => setEditedNotes(e.target.value)}
                  rows={4}
                  placeholder="Optional notes, context, or comments..."
                  disabled={loading}
                />
              </div>

              <div className="task-detail-field">
                <label className="task-detail-label">
                  Review Comment
                </label>
                <textarea
                  className="task-detail-textarea"
                  value={editedReviewComment}
                  onChange={(e) => setEditedReviewComment(e.target.value)}
                  rows={3}
                  placeholder="Feedback when sending back from Review..."
                  disabled={loading}
                />
              </div>

              <div className="task-detail-field">
                <label className="task-detail-label">
                  Commit SHA / PR Link
                </label>
                <input
                  type="text"
                  className="task-detail-select"
                  value={editedCommitSha}
                  onChange={(e) => setEditedCommitSha(e.target.value)}
                  placeholder="Optional commit hash or PR URL..."
                  disabled={loading}
                  style={{ fontFamily: 'monospace', fontSize: '0.9rem' }}
                />
              </div>

              <div className="task-detail-row">
                <div className="task-detail-field">
                  <label className="task-detail-label">Status</label>
                  <select
                    className="task-detail-select"
                    value={editedStatus}
                    onChange={(e) =>
                      setEditedStatus(e.target.value as TaskStatus)
                    }
                    disabled={loading}
                  >
                    {KANBAN_STATUSES.map((s) => (
                      <option key={s} value={s}>
                        {TASK_STATUS_LABELS[s]}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="task-detail-field">
                  <label className="task-detail-label">Priority</label>
                  <select
                    className="task-detail-select"
                    value={editedPriority || ''}
                    onChange={(e) =>
                      setEditedPriority(
                        e.target.value
                          ? (e.target.value as TaskPriority)
                          : null
                      )
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

                <div className="task-detail-field">
                  <label className="task-detail-label">Category</label>
                  <input
                    type="text"
                    className="task-detail-select"
                    value={editedCategory}
                    onChange={(e) => setEditedCategory(e.target.value)}
                    placeholder="e.g. ui, api, database..."
                    disabled={loading}
                  />
                </div>
              </div>

              <div className="task-detail-field">
                <label className="task-detail-label">Task Type</label>
                {editedTaskType === 'proposal' ? (
                  <div className="task-detail-value">
                    <span className="task-detail-type-badge">{TASK_TYPE_LABELS.proposal}</span>
                  </div>
                ) : (
                  <label className="task-type-toggle">
                    <input
                      type="checkbox"
                      checked={editedTaskType === 'agent'}
                      onChange={(e) => setEditedTaskType(e.target.checked ? 'agent' : 'manual')}
                      disabled={loading}
                    />
                    <span className="task-type-slider" aria-hidden="true" />
                    <span className="task-type-text">
                      {TASK_TYPE_LABELS[editedTaskType]}
                    </span>
                  </label>
                )}
              </div>
            </>
          ) : (
            <>
              {task.title && (
                <div className="task-detail-field">
                  <label className="task-detail-label">Title</label>
                  <div className="task-detail-value">{task.title}</div>
                </div>
              )}

              <div className="task-detail-field">
                <label className="task-detail-label">Task Description</label>
                <div className="task-detail-text">{task.text}</div>
              </div>

              {currentNotes && (
                <div className="task-detail-field">
                  <label className="task-detail-label">Notes</label>
                  <div className="task-detail-text" data-testid="task-notes">
                    {parseNotesLines(currentNotes).map((line) =>
                      line.kind === 'checklist' ? (
                        <label key={line.lineIndex} className="task-detail-checklist-item">
                          {line.indent}
                          <input
                            type="checkbox"
                            checked={line.checked}
                            disabled={togglePending}
                            onChange={(e) =>
                              handleChecklistToggle(line.lineIndex, line.text, e.target.checked)
                            }
                          />{' '}
                          <span>{line.text}</span>
                          {'\n'}
                        </label>
                      ) : (
                        <span key={line.lineIndex}>
                          {line.raw}
                          {'\n'}
                        </span>
                      )
                    )}
                  </div>
                  {toggleMessage && (
                    <div className="task-detail-checklist-message" role="status">
                      {toggleMessage}
                    </div>
                  )}
                </div>
              )}

              {task.review_comment && (
                <div className="task-detail-field">
                  <label className="task-detail-label">Review Comment</label>
                  <div className="task-detail-review-comment">{task.review_comment}</div>
                </div>
              )}

              <div className="task-detail-row">
                <div className="task-detail-field">
                  <label className="task-detail-label">Project</label>
                  <div className="task-detail-value">
                    {project?.name || task.project_id}{' '}
                    {project?.checkout && <CheckoutBadge projectId={task.project_id} checkout={project.checkout} />}
                  </div>
                </div>

                <div className="task-detail-field">
                  <label className="task-detail-label">Status</label>
                  <div className="task-detail-value">{TASK_STATUS_LABELS[task.status]}</div>
                </div>

                <div className="task-detail-field">
                  <label className="task-detail-label">Task Type</label>
                  <div className="task-detail-value">
                    <span className="task-detail-type-badge">
                      {TASK_TYPE_LABELS[task.task_type]}
                    </span>
                  </div>
                </div>
              </div>

              <div className="task-detail-row">
                <div className="task-detail-field">
                  <label className="task-detail-label">Priority</label>
                  <div className="task-detail-value">
                    {task.priority ? (
                      <span
                        className="task-detail-priority-badge"
                        style={{
                          backgroundColor:
                            priorityColors[task.priority] || 'transparent',
                        }}
                      >
                        {task.priority}
                      </span>
                    ) : (
                      'None'
                    )}
                  </div>
                </div>

                {task.category && (
                  <div className="task-detail-field">
                    <label className="task-detail-label">Category</label>
                    <div className="task-detail-value">
                      <span className="task-detail-category-badge">
                        {task.category}
                      </span>
                    </div>
                  </div>
                )}
              </div>

              <div className="task-detail-field">
                <label className="task-detail-label">Created</label>
                <div className="task-detail-value">
                  {formatDate(task.created_at)}
                </div>
              </div>

              {task.updated_at !== task.created_at && (
                <div className="task-detail-field">
                  <label className="task-detail-label">Last Updated</label>
                  <div className="task-detail-value">
                    {formatDate(task.updated_at)}
                  </div>
                </div>
              )}

              {task.completed_at && (
                <div className="task-detail-field">
                  <label className="task-detail-label">Completed</label>
                  <div className="task-detail-value">
                    {formatDate(task.completed_at)}
                  </div>
                </div>
              )}

              {task.commit_sha && (
                <div className="task-detail-field">
                  <label className="task-detail-label">Commit/PR</label>
                  <div className="task-detail-value" style={{ fontFamily: 'monospace' }} title={task.commit_sha}>
                    {task.commit_sha.substring(0, 12)}
                  </div>
                </div>
              )}

              {/* Parent task indicator (Task #4645) */}
              {parentDisplayId && (
                <div className="task-detail-field">
                  <label className="task-detail-label">Parent Task</label>
                  <div className="task-detail-value">
                    <span className="task-detail-link">#{parentDisplayId}</span>
                    {task.parent && <span className="task-detail-muted"> - {task.parent.text.substring(0, 50)}</span>}
                  </div>
                </div>
              )}

              {/* Subtasks list (Task #4645) */}
              {task.subtasks && task.subtasks.length > 0 && (
                <div className="task-detail-field">
                  <label className="task-detail-label">
                    Subtasks ({task.subtask_progress?.done}/{task.subtask_progress?.total})
                  </label>
                  <div className="task-detail-subtasks">
                    {task.subtasks.map((subtask) => (
                      <div key={subtask.id} className="task-detail-subtask-item">
                        <span className={`subtask-status-${subtask.status.toLowerCase().replace(' ', '-')}`}>
                          {subtask.status === 'Done' ? '✅' : subtask.status === 'In Progress' ? '●' : '○'}
                        </span>
                        <span>#{subtask.display_id ?? subtask.id} - {subtask.text.substring(0, 60)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Blocking tasks (Task #4579) */}
              {task.blocking_tasks && task.blocking_tasks.length > 0 && (
                <div className="task-detail-field">
                  <label className="task-detail-label">
                    {task.is_blocked ? '🔒 Blocked By (incomplete)' : 'Blocked By'}
                  </label>
                  <div className="task-detail-blocking-tasks">
                    {task.blocking_tasks.map((blocker) => (
                      <div key={blocker.id} className={`task-detail-blocker-item ${blocker.status !== 'Done' ? 'blocker-incomplete' : ''}`}>
                        <span>#{blocker.display_id ?? blocker.id} - {blocker.text.substring(0, 60)}</span>
                        <span className="blocker-status">[{blocker.status}]</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {/* Attachments panel (#5216) */}
        <div className="task-detail-attachments">
          <label className="task-detail-label">Attachments</label>

          {/* Drop zone */}
          <div
            className={`attachment-zone${dragOver ? ' attachment-zone--over' : ''}`}
            onClick={() => fileInputRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleFileDrop}
          >
            {uploading ? '⏳ Uploading…' : '📎 Click or drag files here'}
          </div>

          <input
            ref={fileInputRef}
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={async (e) => {
              if (!e.target.files?.length) return;
              const files = Array.from(e.target.files);
              e.target.value = '';
              handleFiles(files);
            }}
          />

          {/* Attachment grid */}
          {attachments.length > 0 && (
            <div className="attachment-grid">
              {attachments.map((att) => {
                const isImage = att.mime_type?.startsWith('image/');
                const src = `/api/attachments/${att.task_id}/${att.stored_name}`;
                const sizeLabel = att.size_bytes > 1024 * 1024
                  ? `${(att.size_bytes / 1024 / 1024).toFixed(1)} MB`
                  : `${Math.round(att.size_bytes / 1024)} KB`;
                return (
                  <div key={att.id} className="attachment-item">
                    {isImage ? (
                      <a href={src} target="_blank" rel="noreferrer">
                        <img src={src} alt={att.filename} className="attachment-thumb" />
                      </a>
                    ) : (
                      <a href={src} target="_blank" rel="noreferrer" className="attachment-file-icon">
                        📄
                      </a>
                    )}
                    <div className="attachment-info">
                      <span className="attachment-name" title={att.filename}>{att.filename}</span>
                      <span className="attachment-size">{sizeLabel}</span>
                    </div>
                    <button
                      className="attachment-delete"
                      title="Remove attachment"
                      onClick={async () => {
                        if (!task) return;
                        try {
                          await deleteAttachment(task.id, att.id);
                          setAttachments(prev => prev.filter(a => a.id !== att.id));
                        } catch (err) {
                          setError(err instanceof Error ? err.message : 'Delete failed');
                        }
                      }}
                    >
                      ×
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="task-detail-modal-actions">
          {isEditing ? (
            <>
              <button
                className="task-detail-button task-detail-button-cancel"
                onClick={handleCancel}
                disabled={loading}
              >
                Cancel
              </button>
              <button
                className="task-detail-button task-detail-button-save"
                onClick={handleSave}
                disabled={loading || !editedText.trim()}
              >
                {loading ? 'Saving...' : 'Save Changes'}
              </button>
            </>
          ) : (
            <>
              {onDelete && (
                <button
                  className="task-detail-button task-detail-button-delete"
                  onClick={handleDelete}
                  disabled={deleting}
                >
                  {deleting ? 'Deleting...' : 'Delete Task'}
                </button>
              )}
              <button
                className="task-detail-button task-detail-button-edit"
                onClick={startEditing}
                disabled={togglePending}
              >
                Edit Task
              </button>
            </>
          )}
        </div>
      </div >
    </div >
  );
}
