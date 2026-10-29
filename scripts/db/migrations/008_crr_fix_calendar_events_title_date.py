"""Add missing DEFAULT '' to calendar_events.title and calendar_events.event_date.

On the Mac Mini, calendar_events was created by an older ensure_schema that
declared these columns as TEXT NOT NULL without a DEFAULT value:

  title      TEXT NOT NULL
  event_date TEXT NOT NULL

This blocks crsql_as_crr() on the Mini. The laptop has DEFAULT '' already
(applied via a later schema.py version), so this migration is a no-op there.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


CRR_TABLES: frozenset[str] = frozenset({"calendar_events"})

_TARGETS: dict[str, list[str]] = {
    "calendar_events": ["title", "event_date"],
}

_INDEXES: dict[str, list[str]] = {
    "calendar_events": [
        "CREATE INDEX IF NOT EXISTS idx_cal_date    ON calendar_events(event_date)",
        "CREATE INDEX IF NOT EXISTS idx_cal_project ON calendar_events(project_id)",
        "CREATE INDEX IF NOT EXISTS idx_cal_status  ON calendar_events(status)",
        "CREATE INDEX IF NOT EXISTS idx_cal_machine ON calendar_events(machine)",
    ],
}

def _needs_fix(conn: sqlite3.Connection, tbl: str, col: str) -> bool:
    for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall():
        name = row[1] if isinstance(row, (list, tuple)) else row["name"]
        notnull = row[3] if isinstance(row, (list, tuple)) else row["notnull"]
        dflt = row[4] if isinstance(row, (list, tuple)) else row["dflt_value"]
        if name == col:
            return bool(notnull) and dflt is None
    return False


def _patch_sql(sql: str, cols: list[str]) -> str:
    for col in cols:
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
    name = f"pre_{migration_num}_crr_fix_cal_title_date_{ts}.db"
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
            print(f"migration 008: backup skipped for {dest} ({err})", file=sys.stderr)
            continue
        print(f"migration 008: backup → {dest}", file=sys.stderr)


def up(conn: sqlite3.Connection) -> None:
    tables_to_fix = [
        tbl for tbl, cols in _TARGETS.items()
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        and any(_needs_fix(conn, tbl, col) for col in cols)
    ]

    if not tables_to_fix:
        print("migration 008: all target columns already have defaults — nothing to do", file=sys.stderr)
        return

    _backup(conn, "008")

    for tbl in tables_to_fix:
        cols = _TARGETS[tbl]
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"migration 008: {tbl!r} vanished from sqlite_master")

        original_sql = row[0] if isinstance(row, (list, tuple)) else row["sql"]
        before = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]

        new_sql = _patch_sql(original_sql, cols)
        new_sql = re.sub(
            r'(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(?:"'
            + re.escape(tbl) + r'"|' + re.escape(tbl) + r')(?=[\s(])',
            rf'\1"{tbl}_new"',
            new_sql,
            count=1,
            flags=re.IGNORECASE,
        )

        col_list = ", ".join(
            f'"{r[1] if isinstance(r, (list, tuple)) else r["name"]}"'
            for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        )

        conn.execute(new_sql)
        conn.execute(f'INSERT INTO "{tbl}_new" ({col_list}) SELECT {col_list} FROM "{tbl}"')
        after = conn.execute(f'SELECT COUNT(*) FROM "{tbl}_new"').fetchone()[0]
        if after != before:
            raise RuntimeError(
                f"migration 008: row count mismatch on {tbl}: before={before} after={after}"
            )

        new_defaults = {
            r[1] if isinstance(r, (list, tuple)) else r["name"]:
            r[4] if isinstance(r, (list, tuple)) else r["dflt_value"]
            for r in conn.execute(f'PRAGMA table_info("{tbl}_new")').fetchall()
        }
        for col in cols:
            if _needs_fix(conn, tbl, col):
                if new_defaults.get(col) != "''":
                    raise RuntimeError(
                        f"migration 008: {tbl}.{col} DEFAULT not applied in _new table "
                        f"(dflt_value={new_defaults.get(col)!r}). "
                        f"Column type may not be TEXT."
                    )

        print(f"migration 008: filled {tbl}_new ({before} rows)", file=sys.stderr)

    for tbl in reversed(tables_to_fix):
        conn.execute(f'DROP TABLE "{tbl}"')

    for tbl in tables_to_fix:
        conn.execute(f'ALTER TABLE "{tbl}_new" RENAME TO "{tbl}"')
        for idx_sql in _INDEXES.get(tbl, []):
            conn.execute(idx_sql)
        print(f"migration 008: renamed {tbl}_new → {tbl}", file=sys.stderr)

    result = conn.execute("PRAGMA integrity_check").fetchone()
    result_val = result[0] if isinstance(result, (list, tuple)) else result["integrity_check"]
    if result_val != "ok":
        raise RuntimeError(f"migration 008: integrity_check failed: {result_val}")

    print("migration 008: defaults patched, integrity verified", file=sys.stderr)
