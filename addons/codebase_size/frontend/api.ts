// The Codebase size API (addons/codebase_size/routes.py) and its report shape.

export interface CodebaseSizeRow {
  project: string;
  code: number;
  tests: number;
  doc_files: number;
  doc_lines: number;
  last_commit: string | null;
  commits_90d: number | null;
  error: string | null;
  score: number | null;
  baseline_lean: number | null;
  change: number | null;
}

export interface CodebaseSizeTotal {
  code: number;
  tests: number;
  doc_files: number;
  doc_lines: number;
  repos: number;
  score: number | null;
}

export interface CodebaseSizeReport {
  baseline_date: string | null;
  latest_date: string | null;
  formula: string;
  rows: CodebaseSizeRow[];
  total: CodebaseSizeTotal;
}

async function codebaseSizeRequest(path: string, init?: RequestInit): Promise<CodebaseSizeReport> {
  const response = await fetch(`/api${path}`, init);
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
