"""Add missing DEFAULT values to tasks and calendar_events for crsql_as_crr().

Migration 004 patched most NOT NULL columns, but two were missed or did not
exist at the time:

- tasks.created_at TEXT NOT NULL  (no DEFAULT on machines where schema.py
  created this table rather than a pre-existing nullable version)
- tasks.updated_at TEXT NOT NULL  (same)
- calendar_events.created_at TEXT NOT NULL  (table was absent on the Mini when
  migration 004 ran; later added via ensure_schema without DEFAULT values)
- calendar_events.updated_at TEXT NOT NULL  (same)

Machines where the column is already nullable or already has a DEFAULT are
unaffected — the regex only patches the specific NOT NULL / no-DEFAULT pattern.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


CRR_TABLES: frozenset[str] = frozenset({"tasks", "calendar_events"})

_TARGETS: dict[str, list[str]] = {
    "tasks": ["created_at", "updated_at"],
    "calendar_events": ["created_at", "updated_at"],
}

_INDEXES: dict[str, list[str]] = {
    "tasks": [
        "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id) WHERE parent_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed_at) WHERE completed_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_tasks_blocked ON tasks(blocked_by) WHERE blocked_by IS NOT NULL",
    ],
    "calendar_events": [
        "CREATE INDEX IF NOT EXISTS idx_cal_date    ON calendar_events(event_date)",
        "CREATE INDEX IF NOT EXISTS idx_cal_project ON calendar_events(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_cal_status  ON calendar_events(status)",
        "CREATE INDEX IF NOT EXISTS idx_cal_machine ON calendar_events(machine)",
    ],
}


def _needs_fix(conn: sqlite3.Connection, tbl: str, col: str) -> bool:
    """True if col is NOT NULL and has no DEFAULT value."""
    for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall():
        name = row[1] if isinstance(row, (list, tuple)) else row["name"]
        notnull = row[3] if isinstance(row, (list, tuple)) else row["notnull"]
        dflt = row[4] if isinstance(row, (list, tuple)) else row["dflt_value"]
        if name == col:
            return bool(notnull) and dflt is None
    return False


def _patch_sql(sql: str, cols: list[str]) -> str:
    """Add DEFAULT '' after NOT NULL for target columns that lack a default."""
    for col in cols:
        # Match: `col TEXT NOT NULL` NOT followed by DEFAULT
        pattern = (
            r"(\b" + re.escape(col) + r"\s+TEXT\s+NOT\s+NULL)"
            r"(?!\s+DEFAULT)"
        )
        sql = re.sub(pattern, r"\1 DEFAULT ''", sql, flags=re.IGNORECASE)
    return sql


def _backup(conn: sqlite3.Connection, migration_num: str) -> None:
    db_path_str = conn.execute("PRAGMA database_list").fetchone()[2]
    if not db_path_str:
        return
    db_path = Path(db_path_str)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"pre_{migration_num}_crr_fix_defaults_{ts}.db"
    # Respect PT_EXTERNAL_BACKUP_DIR for test isolation (conftest sets this).
    # Treat empty string as unset (fall back to default).
    # governance: allow-silent SF003: the override is optional; empty falls back to the default ~/.project-tracker/backups on the next line
    external_backup_env = os.getenv("PT_EXTERNAL_BACKUP_DIR", "").strip()
    external_backup_dir = Path(external_backup_env) if external_backup_env else Path.home() / ".project-tracker" / "backups"
    for dest in [
        db_path.parent / "backups" / name,
        external_backup_dir / name,
    ]:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            bconn = sqlite3.connect(db_path_str)
            try:
                bconn.execute(f"VACUUM INTO '{str(dest).replace(chr(39), chr(39)*2)}'")
            finally:
                bconn.close()
        except (OSError, sqlite3.OperationalError) as err:
            print(f"migration 007: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        print(f"migration 007: backup → {dest}", file=sys.stderr)


_TRIGGER_NAMES = ["audit_task_delete", "block_task_delete", "audit_project_delete"]


def up(conn: sqlite3.Connection) -> None:
    tables_to_fix = [
        tbl for tbl, cols in _TARGETS.items()
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        and any(_needs_fix(conn, tbl, col) for col in cols)
    ]

    if not tables_to_fix:
        print("migration 007: all target columns already have defaults — nothing to do", file=sys.stderr)
        return

    # Save trigger SQL before any table is dropped — DROP TABLE silently removes triggers.
    saved_triggers: dict[str, str] = {}
    for name in _TRIGGER_NAMES:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).fetchone()
        if row:
            saved_triggers[name] = row[0] if isinstance(row, (list, tuple)) else row["sql"]

    _backup(conn, "007")

    for tbl in tables_to_fix:
        cols = _TARGETS[tbl]
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"migration 007: {tbl!r} vanished from sqlite_master")

        original_sql = row[0] if isinstance(row, (list, tuple)) else row["sql"]
        before = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]

        new_sql = _patch_sql(original_sql, cols)
        # Handle both quoted ("tasks") and unquoted (tasks) table names from sqlite_master.
        # \b fails after a closing " (non-word char), so use a lookahead instead.
        new_sql = re.sub(
            r'(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(?:"'
            + re.escape(tbl) + r'"|' + re.escape(tbl) + r')(?=[\s(])',
            rf'\1"{tbl}_new"',
            new_sql,
            count=1,
            flags=re.IGNORECASE,
        )

        col_list = ", ".join(
            f'"{r[1] if isinstance(r, (list,tuple)) else r["name"]}"'
            for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        )

        conn.execute(new_sql)
        conn.execute(f'INSERT INTO "{tbl}_new" ({col_list}) SELECT {col_list} FROM "{tbl}"')
        after = conn.execute(f'SELECT COUNT(*) FROM "{tbl}_new"').fetchone()[0]
        if after != before:
            raise RuntimeError(
                f"migration 007: row count mismatch on {tbl}: before={before} after={after}"
            )

        # Verify DEFAULT was actually written — catches type-mismatch silent failures.
        new_defaults = {
            r[1] if isinstance(r, (list, tuple)) else r["name"]:
            r[4] if isinstance(r, (list, tuple)) else r["dflt_value"]
            for r in conn.execute(f'PRAGMA table_info("{tbl}_new")').fetchall()
        }
        for col in cols:
            if _needs_fix(conn, tbl, col):  # original table still exists here
                if new_defaults.get(col) != "''":
                    raise RuntimeError(
                        f"migration 007: {tbl}.{col} DEFAULT not applied in _new table "
                        f"(dflt_value={new_defaults.get(col)!r}). "
                        f"Column type may not be TEXT."
                    )

        print(f"migration 007: filled {tbl}_new ({before} rows)", file=sys.stderr)

    for tbl in reversed(tables_to_fix):
        conn.execute(f'DROP TABLE "{tbl}"')

    for tbl in tables_to_fix:
        conn.execute(f'ALTER TABLE "{tbl}_new" RENAME TO "{tbl}"')
        for idx_sql in _INDEXES.get(tbl, []):
            conn.execute(idx_sql)
        print(f"migration 007: renamed {tbl}_new → {tbl}", file=sys.stderr)

    for name, trigger_sql in saved_triggers.items():
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(trigger_sql)
    if saved_triggers:
        print(f"migration 007: restored {len(saved_triggers)} trigger(s)", file=sys.stderr)

    result = conn.execute("PRAGMA integrity_check").fetchone()
    result_val = result[0] if isinstance(result, (list, tuple)) else result["integrity_check"]
    if result_val != "ok":
        raise RuntimeError(f"migration 007: integrity_check failed: {result_val}")

    print("migration 007: defaults patched, integrity verified", file=sys.stderr)
