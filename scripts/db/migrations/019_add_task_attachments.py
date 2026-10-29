"""Create task_attachments as a migration (#8093, plan 4.5).

The table used to be created at runtime by
DatabaseManager.migrate_attachments_table on every init and before each
attachment operation, outside both the schema and the migration ledger. This
migration takes it over so every table comes from one path.

The DDL is the live shape, which carries the DEFAULTs migration 004 added
when it rebuilt the table. IF NOT EXISTS makes this a no-op on any database
that already has it.
"""

from __future__ import annotations

import sqlite3

DDL = (
    """
    CREATE TABLE IF NOT EXISTS task_attachments (
        id           INTEGER PRIMARY KEY NOT NULL,
        task_id      INTEGER NOT NULL DEFAULT 0,
        filename     TEXT NOT NULL DEFAULT '',
        stored_name  TEXT NOT NULL DEFAULT '',
        mime_type    TEXT,
        size_bytes   INTEGER,
        uploaded_at  TEXT DEFAULT (datetime('now')),
        uploaded_by  TEXT DEFAULT 'user'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_attachments_task_id ON task_attachments(task_id)",
)


def up(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)
