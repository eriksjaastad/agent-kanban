"""Codebase size run storage for `pt size` (#8083).

Scanning and scoring live in ``scripts/codebase_size.py`` and have no database
code; this mixin only stores and reads ``codebase_size_snapshots``.

The table is append-only: rows are never updated or deleted (migration 017
adds triggers that abort either), so a stored measurement cannot be lost or
altered. Each ``pt size --snapshot`` inserts one complete run under a new
``run_id`` in a single transaction. The baseline is the earliest run and the
current measurement the newest; a same-day re-run simply adds a run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable

_INSERT = """
    INSERT INTO codebase_size_snapshots
        (run_id, snapshot_date, project, code_lines, test_lines, doc_files,
         doc_lines, last_commit, commits_90d, scanned_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_COLUMNS = (
    "project, code_lines, test_lines, doc_files, doc_lines, last_commit, commits_90d"
)


def _check_rows(rows: list) -> None:
    """Refuse a set that is not one complete measurement, before any write."""
    if not rows:
        raise ValueError("refusing to store an empty snapshot")
    bad = [r.project for r in rows if r.error]
    if bad:
        raise ValueError(f"refusing to store errored repos: {', '.join(bad)}")
    projects = [r.project for r in rows]
    dupes = sorted({p for p in projects if projects.count(p) > 1})
    if dupes:
        raise ValueError(f"refusing to store duplicate repos: {', '.join(dupes)}")


def _read_run(conn, newest: bool):
    """(date, {project: RepoSize}) of the newest or earliest run on ``conn``."""
    from scripts.codebase_size import RepoSize

    order = "DESC" if newest else "ASC"
    edge = conn.execute(
        "SELECT run_id, snapshot_date FROM codebase_size_snapshots "
        f"ORDER BY id {order} LIMIT 1"
    ).fetchone()
    if edge is None:
        return None, {}
    run_id, date = edge[0], edge[1]
    found = conn.execute(
        f"SELECT {_COLUMNS} FROM codebase_size_snapshots WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return date, {r[0]: RepoSize(*tuple(r)) for r in found}


class CodebaseSizeMixin:
    """Codebase size run operations exposed through the shared DatabaseManager."""

    def save_codebase_scan(self, rows: Iterable, snapshot_date: str) -> str:
        """Append ``rows`` as one new run, atomically; return its run_id.

        Errored rows, duplicates and an empty set are refused before any
        write: an unmeasured repo must not be stored as zero lines.
        """
        rows = list(rows)
        _check_rows(rows)
        run_id = uuid.uuid4().hex
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._get_conn() as conn:
            for r in rows:
                conn.execute(_INSERT, (
                    run_id, snapshot_date, r.project, r.code, r.tests, r.doc_files,
                    r.doc_lines, r.last_commit, r.commits_90d, stamp,
                ))
            conn.commit()
        return run_id

    def codebase_runs(self):
        """(baseline_date, baseline, latest_date, latest) from ONE read transaction.

        The single read path for `pt size` and the /codebase API. Both runs come
        from the same database snapshot (WAL), so a run committed by a
        concurrent refresh can never show up as the latest run while the
        baseline read missed it. Each side is (None, {}) when no run exists.
        """
        with self._get_conn() as conn:
            conn.execute("BEGIN")
            try:
                base_date, base = _read_run(conn, newest=False)
                latest_date, latest = _read_run(conn, newest=True)
            finally:
                conn.rollback()  # read-only: end the snapshot
        return base_date, base, latest_date, latest

    def codebase_latest_scan(self):
        """(date, {project: RepoSize}) of the newest run, or (None, {})."""
        _, _, date, rows = self.codebase_runs()
        return date, rows

    def codebase_baseline(self):
        """(date, {project: RepoSize}) of the earliest run, or (None, {})."""
        date, rows, _, _ = self.codebase_runs()
        return date, rows
