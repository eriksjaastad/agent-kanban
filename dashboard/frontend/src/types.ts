// Task and related types for Kanban Board

export type TaskStatus =
  | 'Backlog'
  | 'To Do'
  | 'In Progress'
  | 'Review'
  | 'Done';

// 'proposal' cards change type only through `pt tasks approve` / `reject`.
export type TaskType = 'manual' | 'agent' | 'proposal';

// UI display statuses (original five-column Kanban)
export const KANBAN_STATUSES: TaskStatus[] = [
  'Backlog',
  'To Do',
  'In Progress',
  'Review',
  'Done',
];

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  Backlog: 'Backlog',
  'To Do': 'To Do',
  'In Progress': 'In Progress',
  Review: 'Review',
  Done: 'Done',
};

export const TASK_TYPE_LABELS: Record<TaskType, string> = {
  manual: 'Manual',
  agent: 'Agent',
  proposal: 'Proposal',
};
export type TaskPriority = 'Critical' | 'High' | 'Medium' | 'Low';

export const TASK_PRIORITIES: TaskPriority[] = ['Critical', 'High', 'Medium', 'Low'];

export interface Task {
  // Snowflake-scale pt_id (#6044): a decimal string, never a JS number.
  // Bare JSON numbers above Number.MAX_SAFE_INTEGER round in JSON.parse,
  // which breaks every PATCH built from the rounded value (#7824).
  id: string;
  display_id?: number | null;
  text: string;
  status: TaskStatus;
  project_id: string;
  priority: TaskPriority | null;
  task_type: TaskType;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  prompt: string | null;
  title: string | null;
  notes: string | null;
  commit_sha: string | null;
  category: string | null;
  review_comment: string | null;

  // Parent-child relationships (Task #4645)
  parent_id: string | null;
  parent_display_id?: number | null;
  subtasks?: Task[];
  subtask_progress?: {
    total: number;
    done: number;
    percent: number;
  };
  parent?: Task;

  // Task dependencies (Task #4579)
  blocked_by: string | null;  // JSON array as string
  blocked_by_ids?: string[];
  blocked_by_display_ids?: number[];
  blocking_tasks?: Task[];
  is_blocked?: boolean;
  incomplete_blocking_ids?: string[];
  incomplete_blocking_display_ids?: number[];
  // Blocker ids that resolve to no task at all. Distinct from
  // incomplete_blocking_ids: those name real unfinished work, these name a
  // broken reference. Both set is_blocked, but only one is fixable by doing
  // the blocking task (#6924).
  unresolved_blocking_ids?: string[];
  sequence_order: number | null;

  // File attachments (#5216)
  attachments?: Attachment[];
}

export interface Attachment {
  // Snowflake-scale pt_id (#6044): decimal strings, same rationale as
  // Task.id above (#7824).
  id: string;
  task_id: string;
  filename: string;
  stored_name: string;
  mime_type: string | null;
  size_bytes: number;
  uploaded_at: string;
  uploaded_by: string;
}

/** A job listing as /api/jobs and /api/jobs/submissions return it. */
export interface Job {
  id: number;
  company: string;
  title: string;
  url: string;
  location: string | null;
  posted_date: string | null;
  first_seen: string;
  category: string;
  source: string;
  deleted_at: string | null;
  raw: string | null;
}

export interface Project {
  id: string;
  name: string;
  path: string;
  status: string;
  can_create_cards?: boolean;
  blocked_card_reason?: string | null;
  /** The open checkout (#8339): the project is developed only on that machine. */
  checkout?: ProjectCheckout | null;
}

export interface ProjectCheckout {
  machine: string;
  machine_label: string;
  holder: string;
  note: string | null;
  checked_out_at: string;
}

export interface NavigationItem {
  id: string;
  label: string;
  href: string;
  match_prefixes: string[];
  navigation_type: 'document' | 'spa';
  active?: boolean;
  children?: NavigationItem[];
}

export interface NavigationResponse {
  title: string;
  items: NavigationItem[];
}

export interface AgenticSeriesEntry {
  date: string;
  review_bounces: number;
  review_promotions: number;
  review_entries: number;
}

export interface AgenticSummaryResponse {
  summary: {
    review_bounces: number;
    review_promotions: number;
    review_entries: number;
    bounce_rate: number;
    promotion_rate: number;
  };
  series: AgenticSeriesEntry[];
  markers: AgenticMarker[];
  markers_error?: string | null;
  date_range: {
    start: string;
    end: string;
  };
  project_id?: string | null;
}

export interface AgenticMarker {
  id: string;       // UUID generated on creation
  date: string;     // ISO YYYY-MM-DD
  label: string;    // Display text (max 120 chars)
  source: 'manual' | 'auto';  // manual = user-created, auto = written by tooling (no producer today)
  agent?: string;   // Which agent the marker is about (optional)
}

// Tool invocation stats (WebSearch tracking)
export interface ToolStatsByDate {
  date: string;
  total: number;
}

export interface ToolStatsByProject {
  date: string;
  project: string;
  count: number;
}

export interface ToolStatsByModel {
  date: string;
  model: string;
  count: number;
}

export interface ToolStatsResponse {
  by_date: ToolStatsByDate[];
  by_project: ToolStatsByProject[];
  by_model: ToolStatsByModel[];
  projects: string[];
  models: string[];
}

// Bash error rate (stuck score)
export interface BashStatsByDate {
  date: string;
  error_rate: number;
  total: number;
  errors: number;
}

export interface BashStatsByProject {
  date: string;
  project: string;
  error_rate: number;
}

export interface BashStatsResponse {
  by_date: BashStatsByDate[];
  by_project: BashStatsByProject[];
  projects: string[];
}

// Ideas (Task #4583)
// id is a decimal string: pt_id values exceed 2^53-1 (#7826).
export interface Idea {
  id: string;
  text: string;
  created_at: string;
  updated_at: string;
}
