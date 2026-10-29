"""Create migrations table for bulk-operation recording sessions (Phase F).

`pt migration start <name>` opens a recording session that captures a
baseline (git HEAD + porcelain status) so that `pt migration finish` can
diff the current tree against the baseline and emit a manifest. The DB
row tracks the lifecycle (started_at, finished_at, status).

Classification: LOCAL_ONLY (by design — see DECISIONS.md)
---------------------------------------------------------------------------
A migration session is anchored to a specific repo's working tree. The
state file at ~/.project-tracker/migrations/<name>.json contains paths
that are only meaningful on the machine that captured them. Replicating
across machines would be a tease, not a resume point. Keep the table
LOCAL_ONLY for the same reason handoffs are.
"""

from __future__ import annotations

import sqlite3

CRR_TABLES: frozenset[str] = frozenset()


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            project_id      TEXT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            status          TEXT NOT NULL DEFAULT 'recording'
                                CHECK(status IN ('recording', 'finished',
                                                  'committed', 'reverted')),
            baseline_head   TEXT,
            manifest_path   TEXT,
            created_by      TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_migrations_name
        ON migrations (name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_migrations_status
        ON migrations (status)
        WHERE status = 'recording'
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_migrations_project
        ON migrations (project_id)
    """)
