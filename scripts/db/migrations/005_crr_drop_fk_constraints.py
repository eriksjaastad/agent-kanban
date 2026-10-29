"""Drop all FK constraint declarations from all 10 CRR tables.

cr-sqlite's crsql_as_crr() rejects tables that declare checked FK constraints.
In a replicated environment, a row from a peer can arrive before its referenced
parent (out-of-order sync), which would violate an enforced FK. cr-sqlite
therefore refuses to manage FK enforcement — referential integrity becomes the
application's responsibility.

What changes:
- FK constraint *declarations* are removed from the DDL (FOREIGN KEY clauses
  and inline REFERENCES clauses).
- FK *columns* (project_id, task_id, parent_id, etc.) are retained. Existing
  data relationships are preserved.
- ON DELETE CASCADE no longer fires automatically at the DB level. The
  application's delete paths must issue explicit deletes for child rows, or
  rely on the existing audit/block triggers.

Two DDL patterns are stripped:
  1. Standalone: `FOREIGN KEY (col) REFERENCES tbl(col) ON DELETE ACTION`
     These appear as trailing table-level constraints preceded by a comma.
  2. Inline: `REFERENCES tbl(col) ON DELETE ACTION` on a column definition
     These appear mid-column-def (e.g. task_attachments.task_id, calendar_event_tasks).

Same three-phase rebuild pattern as 003 and 004.
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

# ON DELETE / ON UPDATE action keywords (including two-word forms)
_ON_ACTION = r"(?:SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION)"
_ON_CLAUSE = r"(?:\s+ON\s+(?:DELETE|UPDATE)\s+" + _ON_ACTION + r")*"


def _strip_fk_constraints(sql: str) -> str:
    """Remove all FK constraint declarations from a CREATE TABLE DDL string.

    Two passes:
    1. Strip standalone table-level `FOREIGN KEY` constraints including the
       comma that precedes them.
    2. Strip inline column-level `REFERENCES tbl(col) [ON DELETE ...]` clauses.
    """
    # Pass 1: standalone FOREIGN KEY constraints
    # Pattern: ,<ws>FOREIGN KEY (<cols>) REFERENCES tbl(cols) [ON DELETE ...]
    sql = re.sub(
        r",\s*FOREIGN\s+KEY\s*\([^)]+\)\s+REFERENCES\s+(?:\"[^\"]+\"|[^\s(]+)\s*\([^)]+\)" + _ON_CLAUSE,
        "",
        sql,
        flags=re.IGNORECASE,
    )

    # Pass 2: inline REFERENCES on a column definition
    # Pattern: REFERENCES tbl(col) [ON DELETE ...]
    sql = re.sub(
        r"\s+REFERENCES\s+(?:\"[^\"]+\"|[^\s(]+)\s*\([^)]+\)" + _ON_CLAUSE,
        "",
        sql,
        flags=re.IGNORECASE,
    )

    return sql


def _make_new_table_sql(original_sql: str, tbl: str) -> str:
    """Patch DDL for the <tbl>_new rebuild table.

    Transformations:
    1. Strip all FK constraint declarations.
    2. Rename CREATE TABLE <tbl> → CREATE TABLE <tbl>_new.
    No FK-ref rewriting needed (we just removed all FKs).
    """
    sql = _strip_fk_constraints(original_sql)
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
        print("migration 005: no DB file path (in-memory?), skipping backup", file=sys.stderr)
        return
    db_path = Path(db_path_str)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"pre_005_crr_drop_fk_{ts}.db"
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
            print(f"migration 005: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        except sqlite3.OperationalError as err:
            print(f"migration 005: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        print(f"migration 005: backup → {dest}", file=sys.stderr)


def _create_and_fill_new(conn: sqlite3.Connection, tbl: str) -> int:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"migration 005: table {tbl!r} not found in sqlite_master")

    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
    col_list = ", ".join(f'"{c}"' for c in cols)

    before = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]

    new_sql = _make_new_table_sql(row[0], tbl)
    conn.execute(new_sql)
    conn.execute(f'INSERT INTO "{tbl}_new" ({col_list}) SELECT {col_list} FROM "{tbl}"')

    after = conn.execute(f'SELECT COUNT(*) FROM "{tbl}_new"').fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"migration 005: row count mismatch on {tbl}: before={before} after_copy={after}"
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
        print("migration 005: no CRR tables found — nothing to rebuild", file=sys.stderr)
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
        print(f"migration 005: filled {tbl}_new ({row_counts[tbl]} rows)", file=sys.stderr)

    for tbl in reversed(tables_to_rebuild):
        conn.execute(f'DROP TABLE "{tbl}"')

    for tbl in tables_to_rebuild:
        conn.execute(f'ALTER TABLE "{tbl}_new" RENAME TO "{tbl}"')
        for idx_sql in _INDEXES.get(tbl, []):
            conn.execute(idx_sql)
        print(f"migration 005: renamed {tbl}_new → {tbl}", file=sys.stderr)

    for sql in saved_triggers.values():
        conn.execute(sql)

    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result[0] != "ok":
        raise RuntimeError(f"migration 005: integrity_check failed: {result[0]}")

    print("migration 005: all tables rebuilt, integrity verified", file=sys.stderr)
