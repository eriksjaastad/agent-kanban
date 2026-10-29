// API client for Kanban Board

import type {
  AgenticMarker,
  AgenticSummaryResponse,
  Attachment,
  BashStatsResponse,
  CodebaseSizeReport,
  Idea,
  NavigationResponse,
  Project,
  Task,
  TaskHistoryResponse,
  TaskPriority,
  TaskStatus,
  TaskType,
  ToolStatsResponse,
} from './types';

const API_BASE = '/api';

async function codebaseSizeRequest(path: string, init?: RequestInit): Promise<CodebaseSizeReport> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      // Body was not JSON; the status line below is the message.
    }
    throw new Error(detail || `Codebase size request returned HTTP ${response.status}`);
  }
  return response.json();
}

export function fetchCodebaseSize(signal?: AbortSignal): Promise<CodebaseSizeReport> {
  return codebaseSizeRequest('/codebase-size', { signal });
}

export function refreshCodebaseSize(signal?: AbortSignal): Promise<CodebaseSizeReport> {
  return codebaseSizeRequest('/codebase-size/refresh', { method: 'POST', signal });
}

export interface TaskPolicyResponse {
  blocked_project_ids: string[];
  blocked_project_reasons: Record<string, string | null>;
}

/**
 * True when `error` is the rejection a `fetch()` produces when its request
 * was cancelled via `AbortController.abort()`. Callers use this to swallow
 * the cancellation quietly (it isn't a real failure) while letting every
 * other error follow the normal error-handling path.
 */
export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}

/**
 * Enhanced fetch with error handling and retry logic.
 */
async function fetchWithErrorHandling(
  url: string,
  options: RequestInit = {},
  retries = 3
): Promise<Response> {
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const response = await fetch(url, options);

      // Retry on 5xx errors
      if (response.status >= 500 && attempt < retries - 1) {
        await new Promise(resolve => setTimeout(resolve, Math.pow(2, attempt) * 1000));
        continue;
      }

      return response;
    } catch (error) {
      // An aborted request (unmount, filter toggle, etc.) will never
      // succeed on retry — rethrow immediately instead of burning the
      // backoff schedule on a request that's already dead.
      if (isAbortError(error) || attempt === retries - 1) {
        throw error;
      }
      await new Promise(resolve => setTimeout(resolve, Math.pow(2, attempt) * 1000));
    }
  }

  throw new Error('Failed to fetch after retries');
}

export async function fetchTasks(projectId?: string, status?: TaskStatus, signal?: AbortSignal): Promise<Task[]> {
  const params = new URLSearchParams();
  if (projectId) params.append('project_id', projectId);
  if (status) params.append('status', status);

  const url = `${API_BASE}/tasks${params.toString() ? '?' + params.toString() : ''}`;

  try {
    const response = await fetchWithErrorHandling(url, { signal });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to fetch tasks: ${response.statusText}`);
    }

    const data = await response.json();
    return data.tasks || [];
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to fetch tasks');
  }
}

export async function updateTask(
  taskId: string,
  updates: {
    text?: string;
    title?: string | null;
    notes?: string | null;
    commit_sha?: string | null;
    category?: string | null;
    review_comment?: string | null;
    status?: TaskStatus;
    priority?: TaskPriority | null;
    task_type?: TaskType;
    parent_id?: string | null;
    blocked_by?: string[] | null;
    /** The notes the edit started from; with `notes`, a stale base is a 409. */
    base_notes?: string | null;
  }
): Promise<Task> {
  try {
    // Not fetchWithErrorHandling: it retries 5xx, and a retried write that had
    // landed would come back as a conflict (or a second status change).
    const response = await fetch(`${API_BASE}/tasks/${taskId}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(updates),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const message = errorData.message || errorData.detail || `Failed to update task: ${response.statusText}`;
      if (response.status === 409) {
        throw new NotesConflictError(message);
      }
      throw new Error(message);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to update task');
  }
}

export async function fetchTask(taskId: string): Promise<Task> {
  const response = await fetchWithErrorHandling(`${API_BASE}/tasks/${taskId}`);
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    throw new Error(errorData.message || errorData.detail || `Failed to fetch task: ${response.statusText}`);
  }
  return response.json();
}

/**
 * Thrown when the server answers 409 because a task's notes changed since the
 * client read them: a checklist toggle whose line moved, or an edit Save
 * whose base notes are stale.
 */
export class NotesConflictError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NotesConflictError';
  }
}

/**
 * Tick or untick one notes checklist line (#7821). The server edits only
 * that line's marker under a compare-and-swap, so the client never sends
 * (and can never clobber with) a whole-notes copy.
 */
export async function toggleChecklistItem(
  taskId: string,
  lineIndex: number,
  expectedText: string,
  checked: boolean,
  baseNotes: string | null
): Promise<Task> {
  // Not fetchWithErrorHandling: it retries 5xx, and a retried toggle that
  // actually landed would be reported back as a failure.
  const response = await fetch(`${API_BASE}/tasks/${taskId}/checklist`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // base_notes is the exact text the checkbox was rendered from; the server
    // refuses (409) if the stored notes differ at all, so a moved or
    // duplicated item can never be toggled by mistake.
    body: JSON.stringify({
      line_index: lineIndex,
      expected_text: expectedText,
      checked,
      base_notes: baseNotes,
    }),
  });
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    const message =
      errorData.message || errorData.detail || `Failed to toggle checklist item: ${response.statusText}`;
    if (response.status === 409) {
      throw new NotesConflictError(message);
    }
    throw new Error(message);
  }
  return response.json();
}

export async function createTask(
  text: string,
  projectId: string,
  status: TaskStatus = 'Backlog',
  priority?: TaskPriority | null,
  taskType: TaskType = 'manual',
  category?: string | null,
  parentId?: string | null,
  blockedBy?: string[] | null,
  // Acceptance criteria, required (#7608): must contain at least one
  // "- [ ] <text>" checklist line -- see utils/checklist.ts.
  notes?: string | null
): Promise<Task> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/tasks`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        text,
        project_id: projectId,
        status,
        priority,
        task_type: taskType,
        category,
        parent_id: parentId,
        blocked_by: blockedBy,
        notes,
      }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to create task: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to create task');
  }
}

export async function deleteTask(taskId: string): Promise<void> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/tasks/${taskId}`, {
      method: 'DELETE',
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to delete task: ${response.statusText}`);
    }
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to delete task');
  }
}

export async function fetchProjects(signal?: AbortSignal): Promise<Project[]> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/projects`, { signal });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to fetch projects: ${response.statusText}`);
    }

    const data = await response.json();
    return data.projects || [];
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to fetch projects');
  }
}

export async function fetchTaskPolicy(signal?: AbortSignal): Promise<TaskPolicyResponse> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/task-policy`, { signal });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to fetch task policy: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to fetch task policy');
  }
}

export async function fetchNavigation(signal?: AbortSignal): Promise<NavigationResponse> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/navigation`, { signal });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(
        errorData.message || errorData.detail || `Failed to fetch navigation: ${response.statusText}`
      );
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to fetch navigation');
  }
}

export async function fetchTaskHistory(days: number = 30, projectId?: string, signal?: AbortSignal): Promise<TaskHistoryResponse> {
  const params = new URLSearchParams();
  params.append('days', days.toString());
  if (projectId) params.append('project_id', projectId);

  const url = `${API_BASE}/tasks/history?${params.toString()}`;

  try {
    const response = await fetchWithErrorHandling(url, { signal });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to fetch history: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to fetch task history');
  }
}

export async function fetchAgenticSummary(days: number = 30, projectId?: string, signal?: AbortSignal): Promise<AgenticSummaryResponse> {
  const params = new URLSearchParams();
  params.append('days', days.toString());
  if (projectId) params.append('project_id', projectId);

  const url = `${API_BASE}/agentic/summary?${params.toString()}`;

  try {
    const response = await fetchWithErrorHandling(url, { signal });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to fetch agentic summary: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to fetch agentic summary');
  }
}

// ==================== TOOL STATS API (WebSearch tracking) ====================

export async function fetchToolStats(days: number = 30, tool: string = 'WebSearch', signal?: AbortSignal): Promise<ToolStatsResponse> {
  const params = new URLSearchParams({ days: days.toString(), tool });
  const response = await fetchWithErrorHandling(`${API_BASE}/tool-stats?${params}`, { signal });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to fetch tool stats');
  }
  return response.json();
}

export async function fetchBashStats(days: number = 30, signal?: AbortSignal): Promise<BashStatsResponse> {
  const params = new URLSearchParams({ days: days.toString() });
  const response = await fetchWithErrorHandling(`${API_BASE}/bash-stats?${params}`, { signal });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to fetch bash stats');
  }
  return response.json();
}

// ==================== AGENTIC MARKERS API (#5009) ====================

export async function fetchMarkers(signal?: AbortSignal): Promise<AgenticMarker[]> {
  const response = await fetchWithErrorHandling(`${API_BASE}/agentic/markers`, { signal });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to fetch markers');
  }
  const data = await response.json();
  return data.markers || [];
}

export async function createMarker(
  date: string,
  label: string,
  agent?: string
): Promise<AgenticMarker> {
  const response = await fetchWithErrorHandling(`${API_BASE}/agentic/markers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ date, label, source: 'manual', agent }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to create marker');
  }
  return response.json();
}

export async function updateMarker(
  id: string,
  updates: { date?: string; label?: string; agent?: string }
): Promise<AgenticMarker> {
  const response = await fetchWithErrorHandling(`${API_BASE}/agentic/markers/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to update marker');
  }
  return response.json();
}

export async function deleteMarker(id: string): Promise<void> {
  const response = await fetchWithErrorHandling(`${API_BASE}/agentic/markers/${id}`, {
    method: 'DELETE',
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to delete marker');
  }
}



export async function fetchIdeas(signal?: AbortSignal): Promise<Idea[]> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/ideas`, { signal });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to fetch ideas: ${response.statusText}`);
    }

    const data = await response.json();
    return data.ideas || [];
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to fetch ideas');
  }
}

export async function createIdea(text: string): Promise<Idea> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/ideas`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ text }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to create idea: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to create idea');
  }
}

export async function updateIdea(ideaId: string, text: string): Promise<Idea> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/ideas/${ideaId}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ text }),
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to update idea: ${response.statusText}`);
    }

    return response.json();
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to update idea');
  }
}

export async function deleteIdea(ideaId: string): Promise<void> {
  try {
    const response = await fetchWithErrorHandling(`${API_BASE}/ideas/${ideaId}`, {
      method: 'DELETE',
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.message || errorData.detail || `Failed to delete idea: ${response.statusText}`);
    }
  } catch (error) {
    if (error instanceof Error) {
      throw error;
    }
    throw new Error('Failed to delete idea');
  }
}

// ==================== ATTACHMENT API (#5216) ====================

export async function fetchAttachments(taskId: string, signal?: AbortSignal): Promise<Attachment[]> {
  const response = await fetchWithErrorHandling(`${API_BASE}/tasks/${taskId}/attachments`, { signal });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to fetch attachments');
  }
  const data = await response.json();
  return data.attachments || [];
}

export async function uploadAttachment(taskId: string, file: File): Promise<Attachment> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch(`${API_BASE}/tasks/${taskId}/attachments`, {
    method: 'POST',
    body: formData,
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to upload attachment');
  }
  return response.json();
}

export async function deleteAttachment(taskId: string, attachmentId: string): Promise<void> {
  const response = await fetchWithErrorHandling(
    `${API_BASE}/tasks/${taskId}/attachments/${attachmentId}`,
    { method: 'DELETE' }
  );
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.detail || 'Failed to delete attachment');
  }
}

// ==================== CALENDAR API ====================

// Event and linked-task ids are decimal strings: pt_id values exceed 2^53-1 (#7826).
export interface CalendarEvent {
  id: string;
  title: string;
  description?: string;
  event_date: string;
  event_time?: string;
  event_type: 'reminder' | 'deadline' | 'milestone' | 'meeting' | 'recurring';
  recurrence?: string;
  project_id?: string;
  machine?: string;
  prompt?: string;
  notify_before_minutes: number;
  status: 'active' | 'done' | 'cancelled';
  created_by?: string;
  created_at: string;
  updated_at: string;
  metadata?: Record<string, unknown>;
  linked_tasks?: { id: string; task_id: string; link_type: string }[];
}

export interface CalendarCronJob {
  id: number;
  project_id: string;
  schedule: string;
  command: string;
  description?: string;
  last_run?: string;
  is_active: boolean;
  machine?: string;
}

export interface ScheduledJob {
  source: 'launchd' | 'cron';
  label: string;
  project: string;
  settings: string;
  schedule: string;
  path: string;
  error: string | null;
  status: string;
  last_exit: number | null;
  running: boolean;
}

export interface ScheduledJobs {
  jobs: ScheduledJob[];
  errors: string[];
}

export interface CreateCalendarEventPayload {
  title: string;
  event_date: string;
  event_time?: string;
  event_type?: string;
  project_id?: string;
  machine?: string;
  prompt?: string;
  description?: string;
  notify_before_minutes?: number;
  recurrence?: string;
  created_by?: string;
}

export async function fetchCalendarEvents(params: {
  days?: number;
  project_id?: string;
  machine?: string;
  event_type?: string;
  include_all?: boolean;
} = {}, signal?: AbortSignal): Promise<CalendarEvent[]> {
  const q = new URLSearchParams();
  if (params.days) q.append('days', String(params.days));
  if (params.project_id) q.append('project_id', params.project_id);
  if (params.machine) q.append('machine', params.machine);
  if (params.event_type) q.append('event_type', params.event_type);
  if (params.include_all) q.append('include_all', 'true');
  const response = await fetchWithErrorHandling(`${API_BASE}/calendar/events?${q}`, { signal });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Failed to fetch calendar events');
  const data = await response.json();
  return data.events || [];
}

export async function createCalendarEvent(payload: CreateCalendarEventPayload): Promise<{ id: string; title: string; event_date: string }> {
  const response = await fetchWithErrorHandling(`${API_BASE}/calendar/events`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Failed to create event');
  return response.json();
}

export async function markCalendarEventDone(id: string): Promise<void> {
  const response = await fetchWithErrorHandling(`${API_BASE}/calendar/events/${id}/done`, { method: 'PATCH' });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Failed to mark done');
}

export async function fetchScheduledJobs(signal?: AbortSignal): Promise<ScheduledJobs> {
  const response = await fetchWithErrorHandling(`${API_BASE}/scheduled-jobs`, { signal });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Failed to fetch scheduled jobs');
  return response.json();
}

export async function fetchCalendarCrons(params: { project_id?: string; machine?: string } = {}, signal?: AbortSignal): Promise<CalendarCronJob[]> {
  const q = new URLSearchParams();
  if (params.project_id) q.append('project_id', params.project_id);
  if (params.machine) q.append('machine', params.machine);
  const response = await fetchWithErrorHandling(`${API_BASE}/calendar/crons?${q}`, { signal });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Failed to fetch cron jobs');
  const data = await response.json();
  return data.cron_jobs || [];
}
