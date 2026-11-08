"""Add calendar_events.end_time for #8786 (focus blocks).

An event with both event_time and end_time is a block, which `pt calendar now`
reports as the current or next one. HH:MM local time, NULL for every event
without an end.

Additive and idempotent: a fresh database already has the column from the
generated schema.sql, so it is added only when missing.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)")}
    if "end_time" not in columns:
        conn.execute("ALTER TABLE calendar_events ADD COLUMN end_time TEXT")
