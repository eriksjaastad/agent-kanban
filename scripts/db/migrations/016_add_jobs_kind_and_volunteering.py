"""Add jobs.kind and jobs.volunteering_since for the Volunteer tab (#101668605654102016).

Volunteer roles share the jobs table's lifecycle — a listing, a dismissal,
repeatable submissions — so they live in it rather than a parallel table.
``kind`` separates them ('job' | 'volunteer'); the Jobs page, stats and
submissions read only 'job' rows. ``volunteering_since`` records when the owner
actually started volunteering somewhere, which moves a role from "Applied"
to "Currently volunteering".

Before this column existed, volunteer roles were stored as rows titled
``Volunteer: <role>`` — entered by hand or imported from any source. The
backfill below converts every such row to ``kind='volunteer'`` and strips
the prefix (case-insensitively). It touches only rows still marked 'job',
so a second run changes nothing.

Classification: LOCAL_ONLY, like migration 013 — ``jobs`` has no sync
contract. Additive only: two ADD COLUMNs and one UPDATE, no rebuild, no
DROP. Fresh databases get both columns from the CREATE TABLE in schema.py,
so each ALTER is skipped when its column already exists.
"""

from __future__ import annotations

import sqlite3
import sys

CRR_TABLES: frozenset[str] = frozenset()

_PREFIX = "Volunteer: "


def up(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if not exists:
        # Only reachable when 016 runs on its own; the runner applies 013
        # first, and schema.py's CREATE TABLE already carries both columns.
        print("migration 016: jobs table does not exist yet — nothing to alter",
              file=sys.stderr)
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "kind" not in columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'job' "
            "CHECK (kind IN ('job', 'volunteer'))"
        )
    if "volunteering_since" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN volunteering_since TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs(kind)")

    # Case-insensitive like the LIKE 'Volunteer: %' it replaces. substr is
    # 1-based: the remainder starts right after the 11-char prefix.
    # A row that is nothing but the prefix keeps its title rather than
    # becoming an empty string.
    start = len(_PREFIX) + 1
    conn.execute(
        f"""UPDATE jobs SET kind = 'volunteer',
              title = CASE WHEN trim(substr(title, {start})) <> ''
                           THEN trim(substr(title, {start})) ELSE title END
            WHERE kind = 'job'
              AND lower(substr(title, 1, {len(_PREFIX)})) = ?""",
        (_PREFIX.lower(),),
    )
