"""Add tasks.archived_at so Done-column retention stops deleting history.

Card #6870. `pt tasks done` used to call `trim_done_tasks(keep=75)`, which
counted Done cards portfolio-wide with no project filter and hard-DELETEd
everything past 75. Busy projects evicted quiet ones; `delete_audit_log`
recorded 1,288 destroyed Done cards (244 from project-tracker alone), and
because the delete also removed the card's `task_history` rows it erased
its own evidence.

Retention is now a display concern: `archive_done_tasks()` stamps
`archived_at` on Done cards past the newest 25 *per project*, and
`get_tasks()` hides them by default. Nothing is deleted; `pt tasks list
--archived` shows them again.

CRR note
--------
`tasks` is a live cr-sqlite CRR table on this machine — `tasks__crsql_clock`
and `tasks__crsql_pks` exist, and `tasks__crsql_{i,u,d}trig` enumerate the
base table's columns literally. A bare `ALTER TABLE tasks ADD COLUMN` would
leave those triggers unaware of `archived_at`, so the column would never
replicate and cr-sqlite's column-index mapping would drift.

The runner already brackets `up()` with `crsql_begin_alter('tasks')` /
`crsql_commit_alter('tasks')` when cr-sqlite is loaded — that is what the
`CRR_TABLES` declaration below buys. `pt db migrate` loads the extension
before applying, and `apply_migration` refuses to run an unbracketed alter
against a table that is actually CRR-ified. See MAC_MINI_SYNC_PLAN.md §2.2.

Idempotent: fresh databases get `archived_at` straight from the CREATE TABLE
in schema.py, so the ALTER is skipped when the column already exists.
"""

from __future__ import annotations

import sqlite3
import sys

CRR_TABLES: frozenset[str] = frozenset({"tasks"})


def up(conn: sqlite3.Connection) -> None:
    tasks_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if not tasks_exists:
        # `pt db migrate` can run against a bare database file, before
        # schema.py has created any tables. schema.py's CREATE TABLE already
        # includes archived_at, so there is nothing to do here.
        print(
            "migration 012: tasks table does not exist yet — nothing to alter",
            file=sys.stderr,
        )
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "archived_at" in columns:
        print(
            "migration 012: tasks.archived_at already present — nothing to do",
            file=sys.stderr,
        )
        return

    conn.execute("ALTER TABLE tasks ADD COLUMN archived_at TEXT")
    print("migration 012: added tasks.archived_at", file=sys.stderr)
