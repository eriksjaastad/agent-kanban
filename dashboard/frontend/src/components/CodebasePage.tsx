import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchCodebaseSize, isAbortError, refreshCodebaseSize } from '../api';
import type { CodebaseSizeReport } from '../types';
import { PageShell } from './PageShell';
import './CodebasePage.css';

const MINUS = '−';
const EMPTY = '—';

/** Signed value; a magnitude that rounds to zero carries no sign (never "-0.0"). */
function signed(value: number | null, digits: number): string {
  if (value === null) return EMPTY;
  const rounded = Number(value.toFixed(digits));
  if (rounded === 0) return (0).toFixed(digits);
  const text = Math.abs(rounded).toFixed(digits);
  return rounded > 0 ? `+${text}` : `${MINUS}${text}`;
}

const formatChange = (value: number | null) => signed(value, 0);
const formatScore = (value: number | null) => signed(value, 1);

// A request that never answers would otherwise hold the one-request lock and
// leave both buttons disabled until a reload. A refresh scans every repo
// (seconds, bounded server-side by per-repo git timeouts), so it gets longer.
const LOAD_TIMEOUT_MS = 30_000;
const REFRESH_TIMEOUT_MS = 180_000;

/** An abort signal that fires after `ms`, and whether that is why it fired. */
function deadline(ms: number) {
  const controller = new AbortController();
  let expired = false;
  const timer = setTimeout(() => { expired = true; controller.abort(); }, ms);
  return {
    controller,
    expired: () => expired,
    clear: () => clearTimeout(timer),
  };
}

export function CodebasePage() {
  const [report, setReport] = useState<CodebaseSizeReport | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);

  // One request at a time: the initial load, Retry and Refresh never overlap,
  // so a response can never land on top of a newer one. Each request holds a
  // token; only the active token may update state or release the lock, and
  // unmount abandons it (which also keeps StrictMode's double mount sound).
  const active = useRef<symbol | null>(null);
  const loadAbort = useRef<AbortController | null>(null);

  const start = useCallback((): symbol | null => {
    if (active.current) return null;
    const token = Symbol('codebase-request');
    active.current = token;
    return token;
  }, []);

  const load = useCallback(async () => {
    const token = start();
    if (!token) return;
    const limit = deadline(LOAD_TIMEOUT_MS);
    loadAbort.current = limit.controller;
    setLoading(true);
    setLoadError(null);
    try {
      const next = await fetchCodebaseSize(limit.controller.signal);
      if (active.current === token) {
        setReport(next);
        // A refresh error from before this load describes data no longer shown.
        setRefreshError(null);
      }
    } catch (failure) {
      if (active.current !== token) return;
      if (limit.expired()) {
        setLoadError(`no answer after ${LOAD_TIMEOUT_MS / 1000}s`);
      } else if (!isAbortError(failure)) {
        setLoadError(failure instanceof Error ? failure.message : 'Codebase size unavailable');
      }
    } finally {
      limit.clear();
      if (active.current === token) {
        active.current = null;
        setLoading(false);
      }
    }
  }, [start]);

  useEffect(() => {
    void load();
    return () => {
      active.current = null;
      loadAbort.current?.abort();
    };
  }, [load]);

  async function refresh() {
    const token = start();
    if (!token) return;
    const limit = deadline(REFRESH_TIMEOUT_MS);
    setScanning(true);
    setRefreshError(null);
    try {
      const next = await refreshCodebaseSize(limit.controller.signal);
      if (active.current !== token) return;
      setReport(next);
      setLoadError(null);
    } catch (failure) {
      if (active.current !== token) return;
      if (limit.expired()) {
        setRefreshError(
          `no answer after ${REFRESH_TIMEOUT_MS / 60_000} minutes; the scan may still finish on the server, so reload to check.`,
        );
      } else {
        setRefreshError(failure instanceof Error ? failure.message : 'Refresh failed');
      }
    } finally {
      limit.clear();
      if (active.current === token) {
        active.current = null;
        setScanning(false);
      }
    }
  }

  const busy = loading || scanning;
  const actions = (
    <button type="button" onClick={() => void refresh()} disabled={busy}>
      {scanning ? 'Scanning…' : 'Refresh'}
    </button>
  );

  return (
    <PageShell
      title="Codebase size"
      subtitle="Lines of code and docs per project, scored against the first recorded run."
      actions={actions}
      mainClassName="codebase-page"
    >
      {loading && !report && <p role="status">Loading codebase size…</p>}
      {loadError && (
        <div role="alert" className="codebase-error">
          <p>Could not load codebase size: {loadError}</p>
          <button type="button" onClick={() => void load()} disabled={busy}>Retry</button>
        </div>
      )}
      {refreshError && (
        <p role="alert" className="codebase-error">
          Refresh failed: {refreshError}
          {report ? ' Showing the previous results.' : ''}
        </p>
      )}
      {report && (
        <>
          <div className="codebase-meta">
            <span>Baseline: {report.baseline_date ?? 'none yet'}</span>
            <span>Latest run: {report.latest_date ?? 'none yet'}</span>
          </div>
          <p className="codebase-formula">{report.formula}</p>
          {report.rows.length === 0 ? (
            <p className="codebase-empty">
              No runs yet {EMPTY} press Refresh to take the first snapshot; it becomes the baseline.
            </p>
          ) : (
            <div className="codebase-table-wrap">
              <table className="codebase-table">
                <thead>
                  <tr>
                    <th scope="col">Project</th>
                    <th scope="col">Code</th>
                    <th scope="col">Tests</th>
                    <th scope="col">Doc files</th>
                    <th scope="col">Doc lines</th>
                    <th scope="col">Last commit</th>
                    <th scope="col">Commits (90d)</th>
                    <th scope="col">Change since baseline</th>
                    <th scope="col">Score</th>
                  </tr>
                </thead>
                <tbody>
                  {report.rows.map(row => (
                    <tr key={row.project}>
                      <th scope="row">{row.project}</th>
                      <td>{row.code}</td>
                      <td>{row.tests}</td>
                      <td>{row.doc_files}</td>
                      <td>{row.doc_lines}</td>
                      <td>{row.last_commit ?? EMPTY}</td>
                      <td>{row.commits_90d ?? EMPTY}</td>
                      <td>{formatChange(row.change)}</td>
                      <td>{formatScore(row.score)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">TOTAL ({report.total.repos} repos)</th>
                    <td>{report.total.code}</td>
                    <td>{report.total.tests}</td>
                    <td>{report.total.doc_files}</td>
                    <td>{report.total.doc_lines}</td>
                    <td>{EMPTY}</td>
                    <td>{EMPTY}</td>
                    <td>{EMPTY}</td>
                    <td>{formatScore(report.total.score)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
        </>
      )}
    </PageShell>
  );
}
