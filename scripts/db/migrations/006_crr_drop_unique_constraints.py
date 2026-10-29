"""Drop CRR-incompatible UNIQUE constraints from projects and project_info.

cr-sqlite allows uniqueness only on the primary key. ``projects`` and
``project_info`` still carry extra UNIQUE constraints after migration 005:

- ``projects.name TEXT UNIQUE NOT NULL``
- ``project_info UNIQUE(project_id, key)``

Migration 006 rebuilds just those two tables without the extra uniqueness,
replaces the dropped autoindexes with ordinary lookup indexes, and preserves
all rows. Application code now enforces best-effort local project-name
uniqueness and project-info upsert semantics.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


CRR_TABLES: frozenset[str] = frozenset({
    "projects",
    "project_info",
})

_REBUILD_ORDER = [
    "projects",
    "project_info",
]

_INDEXES: dict[str, list[str]] = {
    "project_info": [
        "CREATE INDEX IF NOT EXISTS idx_project_info_project ON project_info(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_project_info_scope_key ON project_info(project_id, key)",
    ],
    "projects": [
        "CREATE INDEX IF NOT EXISTS idx_projects_last_modified ON projects(last_modified DESC)",
        "CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(name)",
        "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)",
    ],
}


def _strip_unique_constraints(sql: str, tbl: str) -> str:
    """Remove the known non-PK UNIQUE constraints from migration-005 DDL."""
    if tbl == "projects":
        sql = re.sub(
            r"\bname\s+TEXT\s+UNIQUE(\s+NOT\s+NULL(?:\s+DEFAULT\s+[^,\n)]+)?)",
            r"name TEXT\1",
            sql,
            flags=re.IGNORECASE,
        )
    elif tbl == "project_info":
        sql = re.sub(
            r",\s*UNIQUE\s*\(\s*project_id\s*,\s*key\s*\)",
            "",
            sql,
            flags=re.IGNORECASE,
        )
    return sql


def _make_new_table_sql(original_sql: str, tbl: str) -> str:
    sql = _strip_unique_constraints(original_sql, tbl)
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
        print("migration 006: no DB file path (in-memory?), skipping backup", file=sys.stderr)
        return
    db_path = Path(db_path_str)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    name = f"pre_006_crr_drop_unique_{ts}.db"
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
                bconn.execute(f"VACUUM INTO '{str(dest).replace(chr(39), chr(39) * 2)}'")
            finally:
                bconn.close()
        except OSError as err:
            print(f"migration 006: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        except sqlite3.OperationalError as err:
            print(f"migration 006: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        print(f"migration 006: backup → {dest}", file=sys.stderr)


def _create_and_fill_new(conn: sqlite3.Connection, tbl: str) -> int:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (tbl,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"migration 006: table {tbl!r} not found in sqlite_master")

    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
    col_list = ", ".join(f'"{c}"' for c in cols)
    before = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]

    new_sql = _make_new_table_sql(row[0], tbl)
    conn.execute(new_sql)
    conn.execute(f'INSERT INTO "{tbl}_new" ({col_list}) SELECT {col_list} FROM "{tbl}"')

    after = conn.execute(f'SELECT COUNT(*) FROM "{tbl}_new"').fetchone()[0]
    if after != before:
        raise RuntimeError(
            f"migration 006: row count mismatch on {tbl}: before={before} after_copy={after}"
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
        print("migration 006: no target tables found — nothing to rebuild", file=sys.stderr)
        return

    _backup(conn)

    row_counts: dict[str, int] = {}
    for tbl in tables_to_rebuild:
        row_counts[tbl] = _create_and_fill_new(conn, tbl)
        print(f"migration 006: filled {tbl}_new ({row_counts[tbl]} rows)", file=sys.stderr)

    for tbl in reversed(tables_to_rebuild):
        conn.execute(f'DROP TABLE "{tbl}"')

    for tbl in tables_to_rebuild:
        conn.execute(f'ALTER TABLE "{tbl}_new" RENAME TO "{tbl}"')
        for idx_sql in _INDEXES.get(tbl, []):
            conn.execute(idx_sql)
        print(f"migration 006: renamed {tbl}_new → {tbl}", file=sys.stderr)

    result = conn.execute("PRAGMA integrity_check").fetchone()
    if result[0] != "ok":
        raise RuntimeError(f"migration 006: integrity_check failed: {result[0]}")

    print("migration 006: all tables rebuilt, integrity verified", file=sys.stderr)
