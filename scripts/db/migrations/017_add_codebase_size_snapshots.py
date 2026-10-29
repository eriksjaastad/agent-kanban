"""Add the codebase_size_snapshots table for #8083.

Each `pt size --snapshot` stores one complete run measuring how much code and
documentation every repo under the projects root carries. The earliest run is
the baseline that the optimization score compares against.

Classification: LOCAL_ONLY. Runs are measured from the laptop's own
checkouts; the Mini has no use for them and replicating them would only create
conflicting copies. The same DDL lives in schema.py for fresh databases;
IF NOT EXISTS makes this migration safe after either creation path.

The table is append-only: triggers abort any UPDATE or DELETE, so a stored
measurement, including the baseline run, can never be lost or altered.
"""

from __future__ import annotations

import sqlite3

CRR_TABLES: frozenset[str] = frozenset()

DDL = (
    """
    CREATE TABLE IF NOT EXISTS codebase_size_snapshots (
        id            INTEGER PRIMARY KEY NOT NULL,
        run_id        TEXT NOT NULL,
        snapshot_date TEXT NOT NULL,
        project       TEXT NOT NULL,
        code_lines    INTEGER NOT NULL,
        test_lines    INTEGER NOT NULL,
        doc_files     INTEGER NOT NULL,
        doc_lines     INTEGER NOT NULL,
        last_commit   TEXT,
        commits_90d   INTEGER,
        scanned_at    TEXT NOT NULL,
        UNIQUE(run_id, project)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS codebase_size_snapshots_no_update
    BEFORE UPDATE ON codebase_size_snapshots
    BEGIN SELECT RAISE(ABORT, 'codebase_size_snapshots is append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS codebase_size_snapshots_no_delete
    BEFORE DELETE ON codebase_size_snapshots
    BEGIN SELECT RAISE(ABORT, 'codebase_size_snapshots is append-only'); END
    """,
)


def up(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)
