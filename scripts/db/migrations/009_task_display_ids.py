"""Create task_display_ids LOCAL_ONLY table for human-readable task numbers.

pt_next_id() generates 16-digit Snowflake IDs for CRR collision-safety, but
these are unreadable to humans. This migration introduces a separate
LOCAL_ONLY table that maps each Snowflake PK to a machine-local sequential
display ID so `pt tasks list` can show #6100 instead of #39846709799579648.

Design decisions:
- The table is LOCAL_ONLY: display IDs are per-machine, never synced.
- display_id is a plain auto-increment counter (MAX + 1 on insert).
- Backfill: pre-Snowflake tasks (id < 2^40, roughly < 1 trillion) get
  display_id = task_id (they were already sequential integers). Snowflake
  tasks get display_id = MAX(existing display_id) + sequential offset.
- The migration is idempotent: re-running is safe (CREATE IF NOT EXISTS +
  INSERT OR IGNORE).
"""

from __future__ import annotations

import sqlite3
import sys

CRR_TABLES: frozenset[str] = frozenset()

_SNOWFLAKE_THRESHOLD = 10 ** 12


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_display_ids (
            task_id    INTEGER PRIMARY KEY NOT NULL,
            display_id INTEGER NOT NULL UNIQUE
        )
    """)

    existing = conn.execute("SELECT COUNT(*) FROM task_display_ids").fetchone()[0]
    if existing > 0:
        print("migration 009: task_display_ids already populated — nothing to backfill", file=sys.stderr)
        return

    tasks_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if not tasks_exists:
        print("migration 009: tasks table does not exist yet — skipping backfill", file=sys.stderr)
        return

    rows = conn.execute("SELECT id FROM tasks ORDER BY id").fetchall()
    if not rows:
        print("migration 009: no tasks to backfill", file=sys.stderr)
        return

    legacy = [(r[0],) for r in rows if r[0] < _SNOWFLAKE_THRESHOLD]
    snowflake = [r[0] for r in rows if r[0] >= _SNOWFLAKE_THRESHOLD]

    conn.executemany(
        "INSERT OR IGNORE INTO task_display_ids (task_id, display_id) VALUES (?, ?)",
        [(task_id, task_id) for (task_id,) in legacy],
    )

    if snowflake:
        max_display = conn.execute("SELECT COALESCE(MAX(display_id), 0) FROM task_display_ids").fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO task_display_ids (task_id, display_id) VALUES (?, ?)",
            [(tid, max_display + i + 1) for i, tid in enumerate(snowflake)],
        )

    total = conn.execute("SELECT COUNT(*) FROM task_display_ids").fetchone()[0]
    print(
        f"migration 009: backfilled {total} task(s) "
        f"({len(legacy)} legacy, {len(snowflake)} snowflake)",
        file=sys.stderr,
    )
