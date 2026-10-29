"""
Create the Phase 2 sync ledgers.

- ``schema_migrations`` (LOCAL_ONLY) — the migration runner's source of
  truth for which migrations have been applied *on this machine*. Must
  stay local: if this table replicated, applying a migration on
  machine A would propagate its row to B before B's runner ran the
  DDL, and B would skip the DDL entirely (phantom migration). See
  ``crr_manifest.py`` and MAC_MINI_SYNC_PLAN.md §2.1.

- ``schema_migration_announcements`` (CONTROL_PLANE) — the cross-machine
  "migration applied here" signal. Written *after* a successful local
  apply, replicated via the always-on control plane even while the
  data plane is paused. ``pt sync resume`` (PR #3) blocks until both
  machines' announcement heads match. See MAC_MINI_SYNC_PLAN.md §2.1.

Both tables are created as ordinary SQLite tables here. They become
LOCAL_ONLY / CONTROL_PLANE only once the sync daemon (Phase 2 PR #3)
loads cr-sqlite and routes them according to the CRR manifest —
until then they behave like any other local table, which is exactly
what we need during bootstrap.

``CRR_TABLES`` is empty because this migration does not ALTER any
already-CRR table; it only CREATEs new ones. The runner's
``crsql_begin_alter`` / ``crsql_commit_alter`` bracketing is for
altering existing CRR tables, not for creating fresh tables that
haven't been flipped to CRR yet.
"""

from __future__ import annotations

import sqlite3


CRR_TABLES: frozenset[str] = frozenset()


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration_announcements (
            version     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            machine_id  TEXT NOT NULL,
            applied_at  TEXT NOT NULL,
            PRIMARY KEY (version, machine_id)
        )
        """
    )
