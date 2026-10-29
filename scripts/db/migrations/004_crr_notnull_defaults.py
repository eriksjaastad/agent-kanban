"""Add DEFAULT values to every NOT NULL non-PK column in all 10 CRR tables.

cr-sqlite's crsql_as_crr() rejects tables where any NOT NULL column lacks a
DEFAULT value. Reason: during partial-row sync, cr-sqlite reconstructs rows
from peer changesets that may only carry a subset of columns — which requires
every NOT NULL column to have a safe fallback value.

Columns with a CHECK constraint (tasks.status) get a DEFAULT that satisfies the
constraint. Integer FK columns (task_history.task_id, task_attachments.task_id)
get DEFAULT 0. All other NOT NULL TEXT columns get DEFAULT ''.

calendar_event_tasks is already compliant (link_type has DEFAULT 'related';
event_id and task_id are PK columns and are excluded from this check).

This migration uses the same three-phase rebuild pattern as 003:
    1. Save trigger SQL.
    2. VACUUM INTO backups (two locations).
    3. Phase 1 — CREATE TABLE <tbl>_new (patched DDL), INSERT INTO SELECT *.
    4. Phase 2 — DROP old tables in reverse FK order.
    5. Phase 3 — RENAME _new → final, recreate indexes.
    6. Restore triggers.
    7. PRAGMA integrity_check.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


CRR_TABLES: frozenset[str] = frozenset({
    "tasks",
    "ideas",
    "task_history",
    "task_attachments",
    "projects",
    "project_info",
    "ai_agents",
    "service_dependencies",
    "calendar_events",
    "calendar_event_tasks",
})

_REBUILD_ORDER = [
    "projects",
    "ideas",
    "ai_agents",
    "service_dependencies",
    "project_info",
    "tasks",
    "calendar_events",
    "task_history",
    "task_attachments",
    "calendar_event_tasks",
]

# DEFAULT value (SQL literal) for every NOT NULL non-PK column that currently
# lacks one. Columns already carrying a DEFAULT are left untouched.
# tasks.status must satisfy its CHECK constraint — 'Backlog' is always valid.
_COLUMN_DEFAULTS: dict[tuple[str, str], str] = {
    ("ai_agents", "project_id"):           "''",
    ("ai_agents", "agent_name"):           "''",
    ("calendar_events", "title"):          "''",
    ("calendar_events", "event_date"):     "''",
    ("calendar_events", "created_at"):     "''",
    ("calendar_events", "updated_at"):     "''",
    ("ideas", "text"):                     "''",
    ("ideas", "created_at"):               "''",
    ("ideas", "updated_at"):               "''",
    ("project_info", "key"):               "''",
    ("project_info", "value"):             "''",
    ("project_info", "updated_at"):        "''",
    ("projects", "name"):                  "''",
    ("projects", "path"):                  "''",
    ("projects", "status"):                "''",
    ("projects", "created_at"):            "''",
    ("service_dependencies", "project_id"): "''",
    ("service_dependencies", "service_name"): "''",
    ("task_attachments", "task_id"):       "0",
    ("task_attachments", "filename"):      "''",
    ("task_attachments", "stored_name"):   "''",
    ("task_history", "task_id"):           "0",
    ("task_history", "project_id"):        "''",
    ("task_history", "event_type"):        "''",
    ("task_history", "timestamp"):         "''",
    ("tasks", "text"):                     "''",
    ("tasks", "status"):                   "'Backlog'",
    ("tasks", "project_id"):               "''",
}

_INDEXES: dict[str, list[str]] = {
    "ai_agents": [
        "CREATE INDEX IF NOT EXISTS idx_ai_agents_project ON ai_agents(project_id)",
    ],
    "calendar_events": [
        "CREATE INDEX IF NOT EXISTS idx_cal_date    ON calendar_events(event_date)",
        "CREATE INDEX IF NOT EXISTS idx_cal_project ON calendar_events(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_cal_status  ON calendar_events(status)",
        "CREATE INDEX IF NOT EXISTS idx_cal_machine ON calendar_events(machine)",
    ],
    "project_info": [
        "CREATE INDEX IF NOT EXISTS idx_project_info_project ON project_info(project_id)",
    ],
    "projects": [
        "CREATE INDEX IF NOT EXISTS idx_projects_last_modified ON projects(last_modified DESC)",
        "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)",
    ],
    "service_dependencies": [
        "CREATE INDEX IF NOT EXISTS idx_service_deps_project ON service_dependencies(project_id)",
    ],
    "task_attachments": [
        "CREATE INDEX IF NOT EXISTS idx_attachments_task_id ON task_attachments(task_id)",
    ],
    "task_history": [
        "CREATE INDEX IF NOT EXISTS idx_history_event ON task_history(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_history_project ON task_history(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_history_timestamp ON task_history(timestamp)",
    ],
    "tasks": [
        "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed_at) WHERE completed_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_tasks_blocked ON tasks(blocked_by) WHERE blocked_by IS NOT NULL",
    ],
}


def _patch_defaults(sql: str, tbl: str) -> str:
    """Insert DEFAULT <value> after NOT NULL for every column in _COLUMN_DEFAULTS.

    Only patches columns that do not already carry a DEFAULT. Uses a negative
    lookahead to skip columns that were already given defaults (e.g. by a prior
    run or by the source DDL).
    """
    for (table, col), default_val in _COLUMN_DEFAULTS.items():
        if table != tbl:
            continue
        # Match:  <col_name>  <TYPE>  NOT NULL  (no DEFAULT following)
        # Handles optional type-params like VARCHAR(255) and appends DEFAULT
        # before any subsequent CHECK(...) or comma or closing paren.
        # Allow optional constraint keywords (e.g. UNIQUE) between the type
        # and NOT NULL: `name TEXT UNIQUE NOT NULL` must still match.
        sql = re.sub(
            r'(\b' + re.escape(col) + r'\b\s+\w+(?:\([^)]*\))?(?:\s+\w+)*\s+NOT\s+NULL\b)'
            r'(?!\s+DEFAULT)',
            r'\1 DEFAULT ' + default_val,
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
    return sql


def _make_new_table_sql(original_sql: str, tbl: str) -> str:
    """Patch DDL for the <tbl>_new rebuild table.

    Transformations:
    1. Add DEFAULT values to NOT NULL non-PK columns that lack them.
    2. Rewrite FK REFERENCES to other CRR tables → _new versions.
    3. Rename CREATE TABLE <tbl> → CREATE TABLE <tbl>_new.
    """
    sql = _patch_defaults(original_sql, tbl)

    for ref_tbl in CRR_TABLES:
        sql = re.sub(
            r"\bREFERENCES\s+(?:\"" + re.escape(ref_tbl) + r"\"|" + re.escape(ref_tbl) + r"\b)\s*\(",
            f'REFERENCES "{ref_tbl}_new"(',
            sql,
            flags=re.IGNORECASE,
        )

    sql = re.sub(
        r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(?:\"" + re.escape(tbl) + r"\"|" + re.escape(tbl) + r"\b)",
        rf'\1"{tbl}_new"',
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    return sql


def _backup(conn: sqlite3.Connection) -> None:
    db_path_str = conn.execute("PRAGMA database_list").fetchone()[2]
    if not db_path_str:
        print("migration 004: no DB file path (in-memory?), skipping backup", file=sys.stderr)
        return
    db_path = Path(db_path_str)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"pre_004_crr_defaults_{ts}.db"
    # Respect PT_EXTERNAL_BACKUP_DIR for test isolation (conftest sets this).
    # Treat empty string as unset (fall back to default).
    # governance: allow-silent SF003: the override is optional; empty falls back to the default ~/.project-tracker/backups on the next line
    external_backup_env = os.getenv("PT_EXTERNAL_BACKUP_DIR", "").strip()
    external_backup_dir = Path(external_backup_env) if external_backup_env else Path.home() / ".project-tracker" / "backups"
    locations = [
        db_path.parent / "backups" / name,
        external_backup_dir / name,
    ]
    for dest in locations:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            bconn = sqlite3.connect(db_path_str)
            try:
                bconn.execute(f"VACUUM INTO '{str(dest).replace(chr(39), chr(39)*2)}'")
            finally:
                bconn.close()
        except OSError as err:
            print(f"migration 004: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        except sqlite3.OperationalError as err:
            print(f"migration 004: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        print(f"migration 004: backup → {dest}", file=sys.stderr)


def _create_and_fill_new(conn: sqlite3.Connection, tbl: str) -> int:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"migration 004: table {tbl!r} not found in sqlite_master")

    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
    col_list = ", ".join(f'"{c}"' for c in cols)

    before = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]

    new_sql = _make_new_table_sql(row[0], tbl)
    conn.execute(new_sql)
    conn.execute(f'INSERT INTO "{tbl}_new" ({col_list}) SELECT {col_list} FROM "{tbl}"')

    after = conn.execute(f'SELECT COUNT(*) FROM "{tbl}_new"').fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"migration 004: row count mismatch on {tbl}: before={before} after_copy={after}"
        )
    return before


def up(conn: sqlite3.Connection) -> None:
    placeholders = ",".join("?" * len(_REBUILD_ORDER))
    existing = {
        r[0]
        for r in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
            _REBUILD_ORDER,
        ).fetchall()
    }
    tables_to_rebuild = [t for t in _REBUILD_ORDER if t in existing]
    if not tables_to_rebuild:
        print("migration 004: no CRR tables found — nothing to rebuild", file=sys.stderr)
        return

    trigger_names = ["audit_task_delete", "block_task_delete", "audit_project_delete"]
    saved_triggers: dict[str, str] = {}
    for name in trigger_names:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if row and row[0]:
            saved_triggers[name] = row[0]

    _backup(conn)

    row_counts: dict[str, int] = {}
    for tbl in tables_to_rebuild:
        row_counts[tbl] = _create_and_fill_new(conn, tbl)
        print(f"migration 004: filled {tbl}_new ({row_counts[tbl]} rows)", file=sys.stderr)

    for tbl in reversed(tables_to_rebuild):
        conn.execute(f'DROP TABLE "{tbl}"')

    for tbl in tables_to_rebuild:
        conn.execute(f'ALTER TABLE "{tbl}_new" RENAME TO "{tbl}"')
        for idx_sql in _INDEXES.get(tbl, []):
            conn.execute(idx_sql)
        print(f"migration 004: renamed {tbl}_new → {tbl}", file=sys.stderr)

    for sql in saved_triggers.values():
        conn.execute(sql)

    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result[0] != "ok":
        raise RuntimeError(f"migration 004: integrity_check failed: {result[0]}")

    print("migration 004: all tables rebuilt, integrity verified", file=sys.stderr)
