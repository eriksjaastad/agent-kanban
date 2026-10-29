"""Remove AUTOINCREMENT and add explicit NOT NULL on PK columns for all 10 CRR tables.

cr-sqlite's ``crsql_as_crr()`` validator refuses to flip a table that:

1. Uses ``AUTOINCREMENT`` — concurrent machines would generate colliding IDs
   (empirically confirmed 2026-04-20: two sites each auto-generated id=3 for
   different rows; sync merged them as the same row, silently overwriting one).
2. Lacks explicit ``NOT NULL`` on PK column(s).

This migration rebuilds each CRR table using SQLite's table-rebuild pattern
(the only way to change a column constraint in SQLite):

    1. Save trigger SQL before any DROP TABLE (DROP TABLE cascades to triggers).
    2. Create backups (VACUUM INTO, two locations) before any modifications.
    3. PRAGMA foreign_keys = OFF so rebuild order doesn't matter.
    4. For each table: read current DDL from sqlite_master, patch PK declaration,
       CREATE TABLE <tbl>_new, INSERT INTO <tbl>_new SELECT * FROM <tbl>,
       verify row count, DROP TABLE <tbl>, RENAME <tbl>_new TO <tbl>.
    5. Recreate indexes and triggers.
    6. PRAGMA integrity_check — raise if not "ok".
    7. FK integrity is verified by the test suite (separate connection with
       PRAGMA foreign_keys = ON), not inline, because PRAGMA foreign_keys
       is a no-op inside a transaction.

This migration does NOT wire pt_id into INSERT call sites — that happens
per-table as each table is flipped to CRR in §2.5. The schema change here
is sufficient to allow crsql_as_crr() to succeed.
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

# Rebuild order: parents before children to follow FK dependency graph
# (PRAGMA foreign_keys = OFF makes this technically optional, but makes
# intent clear and matches the order §2.5 will flip tables to CRR).
_REBUILD_ORDER = [
    "projects",          # no CRR deps
    "ideas",             # no CRR deps
    "ai_agents",         # → projects
    "service_dependencies",  # → projects
    "project_info",      # → projects (nullable)
    "tasks",             # → projects  [must precede task_history, task_attachments]
    "calendar_events",   # → projects  [must precede calendar_event_tasks]
    "task_history",      # → tasks, projects
    "task_attachments",  # → tasks
    "calendar_event_tasks",  # → calendar_events, tasks
]

# Indexes to recreate after each table's DROP/RENAME cycle.
# Captured from live DB on 2026-04-20. Partial indexes (WHERE clause) are
# included verbatim so conditional filtering is preserved.
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


def _patch_pk_ddl(sql: str) -> str:
    """Replace AUTOINCREMENT with NOT NULL and add NOT NULL to bare TEXT PRIMARY KEY."""
    sql = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "INTEGER PRIMARY KEY NOT NULL",
        sql,
        flags=re.IGNORECASE,
    )
    # TEXT PRIMARY KEY (projects.id) — add NOT NULL if not already present
    sql = re.sub(
        r"\bTEXT\s+PRIMARY\s+KEY\b(?!\s+NOT\s+NULL)",
        "TEXT PRIMARY KEY NOT NULL",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _make_new_table_sql(original_sql: str, tbl: str) -> str:
    """Patch original CREATE TABLE DDL for the <tbl>_new rebuild table.

    Three transformations:
    1. Remove AUTOINCREMENT, add NOT NULL to PK columns.
    2. Rename CREATE TABLE <tbl> → CREATE TABLE <tbl>_new.
    3. Rewrite REFERENCES <other_crr_table>(...) to REFERENCES <other_crr_table>_new(...)
       so that during the drop phase (phase 2) cascade triggers from _new tables never
       fire against already-dropped old tables. SQLite's RENAME TO auto-corrects the
       _new suffixes back to final names as each table is renamed in phase 3.
    """
    sql = _patch_pk_ddl(original_sql)
    # Rewrite FK references to CRR tables to point at their _new versions.
    # Must happen before the table rename so we don't accidentally match the
    # just-renamed table itself.
    for ref_tbl in CRR_TABLES:
        sql = re.sub(
            r"\bREFERENCES\s+(?:\"" + re.escape(ref_tbl) + r"\"|" + re.escape(ref_tbl) + r"\b)\s*\(",
            f'REFERENCES "{ref_tbl}_new"(',
            sql,
            flags=re.IGNORECASE,
        )
    # Rename CREATE TABLE <tbl> → CREATE TABLE <tbl>_new (quoted or bare form).
    sql = re.sub(
        r"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(?:\"" + re.escape(tbl) + r"\"|" + re.escape(tbl) + r"\b)",
        rf'\1"{tbl}_new"',
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    return sql


def _backup(conn: sqlite3.Connection) -> None:
    """Create two VACUUM INTO backups before any modifications."""
    db_path_str = conn.execute("PRAGMA database_list").fetchone()[2]
    if not db_path_str:
        print("migration 003: no DB file path (in-memory?), skipping backup", file=sys.stderr)
        return
    db_path = Path(db_path_str)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"pre_003_crr_pk_{ts}.db"
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
            # New connection so VACUUM INTO runs outside the open transaction.
            bconn = sqlite3.connect(db_path_str)
            try:
                bconn.execute(f"VACUUM INTO '{str(dest).replace(chr(39), chr(39)*2)}'")
            finally:
                bconn.close()
        except OSError as err:
            print(f"migration 003: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        except sqlite3.OperationalError as err:
            print(f"migration 003: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        print(f"migration 003: backup → {dest}", file=sys.stderr)


def _create_and_fill_new(conn: sqlite3.Connection, tbl: str) -> int:
    """Phase 1: CREATE TABLE <tbl>_new and copy rows. Returns row count.

    Does NOT drop the old table yet — we drop all old tables together in
    phase 2 (reverse FK order) so FK CASCADE triggers never fire during the
    drop phase (children are dropped before parents).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
    ).fetchone()
    if row is None:
        raise RuntimeError(f"migration 003: table {tbl!r} not found in sqlite_master")
    original_sql = row[0]

    # Dynamic column list handles ADD COLUMN drift between machines.
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
    col_list = ", ".join(f'"{c}"' for c in cols)

    before = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]

    new_sql = _make_new_table_sql(original_sql, tbl)
    conn.execute(new_sql)
    conn.execute(f'INSERT INTO "{tbl}_new" ({col_list}) SELECT {col_list} FROM "{tbl}"')

    after = conn.execute(f'SELECT COUNT(*) FROM "{tbl}_new"').fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"migration 003: row count mismatch on {tbl}: before={before} after_copy={after}"
        )
    return before


def up(conn: sqlite3.Connection) -> None:
    # Determine which CRR tables actually exist (fresh installs may have none yet;
    # schema.py already creates them with the correct NOT NULL DDL in that case).
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
        print("migration 003: no CRR tables found — nothing to rebuild", file=sys.stderr)
        return

    # Save all trigger SQL before any DROP TABLE (DROP cascades to triggers).
    trigger_names = ["audit_task_delete", "block_task_delete", "audit_project_delete"]
    saved_triggers: dict[str, str] = {}
    for name in trigger_names:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if row and row[0]:
            saved_triggers[name] = row[0]

    _backup(conn)

    # Phase 1: create <tbl>_new and fill — no drops yet.
    row_counts: dict[str, int] = {}
    for tbl in tables_to_rebuild:
        row_counts[tbl] = _create_and_fill_new(conn, tbl)
        print(f"migration 003: filled {tbl}_new ({row_counts[tbl]} rows)", file=sys.stderr)

    # Phase 2: drop old tables in reverse FK order (children before parents) so
    # ON DELETE CASCADE triggers never fire during the drop phase.
    for tbl in reversed(tables_to_rebuild):
        conn.execute(f'DROP TABLE "{tbl}"')

    # Phase 3: rename _new → final names and recreate indexes.
    for tbl in tables_to_rebuild:
        conn.execute(f'ALTER TABLE "{tbl}_new" RENAME TO "{tbl}"')
        for idx_sql in _INDEXES.get(tbl, []):
            conn.execute(idx_sql)
        print(f"migration 003: renamed {tbl}_new → {tbl}", file=sys.stderr)

    # Restore triggers (dropped in phase 2 along with their tables).
    for sql in saved_triggers.values():
        conn.execute(sql)

    # Integrity check (FK check happens implicitly at COMMIT via defer_foreign_keys).
    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result[0] != "ok":
        raise RuntimeError(f"migration 003: integrity_check failed: {result[0]}")

    print("migration 003: all tables rebuilt, integrity verified", file=sys.stderr)
