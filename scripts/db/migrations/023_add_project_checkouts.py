"""Add the project_checkouts table for #8339 (which machine a project is out to).

A checkout says a project is being developed on one machine (the MacBook or
the Mac Mini) by a named holder, so agents on the other machine leave its code
alone. It can last weeks, so there is no heartbeat or expiry: it stays open
until someone checks it in, and the check-in records who and when. Rows are
kept after check-in as the history.

The partial unique index allows at most one open checkout per project, so a
second checkout is refused by the database itself, not by a read-then-write.

Classification: LOCAL_ONLY, like every table since migration 018. The same
DDL reaches fresh databases through the generated schema.sql; IF NOT EXISTS
makes this migration a no-op on a database that already has it.
"""

from __future__ import annotations

import sqlite3

DDL = (
    """
    CREATE TABLE IF NOT EXISTS project_checkouts (
        id             INTEGER PRIMARY KEY NOT NULL,
        project_id     TEXT NOT NULL,
        machine        TEXT NOT NULL,
        holder         TEXT NOT NULL,
        note           TEXT,
        checked_out_at TEXT NOT NULL,
        checked_in_at  TEXT,
        checked_in_by  TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_project_checkouts_open
        ON project_checkouts (project_id) WHERE checked_in_at IS NULL
    """,
)


def up(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)
