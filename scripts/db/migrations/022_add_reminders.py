"""Add the reminders table for #8282 (the dashboard's Reminders page).

Each row is one thing the user wants to remember they have: a tool, guide, research
note, tip or decision, with why or when to reach for it and where it lives.
An item retires when its content is built into a process, skill or tool
(built_in_at) or when the user removes it (deleted_at). Rows are never
hard-deleted, so a retired or removed reminder can still be found.

Classification: LOCAL_ONLY, like every table since migration 018. The same
DDL reaches fresh databases through the generated schema.sql; IF NOT EXISTS
makes this migration a no-op on a database that already has it.
"""

from __future__ import annotations

import sqlite3

DDL = (
    """
    CREATE TABLE IF NOT EXISTS reminders (
        id          INTEGER PRIMARY KEY NOT NULL,
        name        TEXT NOT NULL,
        purpose     TEXT,
        location    TEXT,
        created_at  TEXT NOT NULL,
        built_in_at TEXT,
        deleted_at  TEXT,
        updated_at  TEXT NOT NULL
    )
    """,
)


def up(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)
