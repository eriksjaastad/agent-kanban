"""Add the task_notes_history audit table for #7642.

Every real change to a task's notes writes one row here, in the same
transaction as the UPDATE that changed them, so a card's acceptance criteria
can never be silently overwritten without leaving evidence.

Classification: LOCAL_ONLY. All card writes happen on the laptop; the Mini
files cards over SSH to the laptop's pt, so notes history never needs to
replicate. Task deletes deliberately leave these rows alone — they are
evidence, not children. The same DDL lives in schema.py for fresh databases;
CREATE IF NOT EXISTS makes this migration safe after either creation path.
There is no foreign key, so a task delete can never cascade into this table.
"""

from __future__ import annotations

import sqlite3

CRR_TABLES: frozenset[str] = frozenset()


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_notes_history (
            id         INTEGER PRIMARY KEY NOT NULL,
            task_id    INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            old_notes  TEXT,
            new_notes  TEXT,
            source     TEXT NOT NULL DEFAULT 'unknown',
            timestamp  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_notes_history_task_timestamp
        ON task_notes_history(task_id, timestamp)
    """)
