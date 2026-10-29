"""Remove cr-sqlite (#8093, decision D2).

Multi-machine sync is retired, so the CRR machinery goes: the three
``<table>__crsql_{i,u,d}trig`` triggers on each replicated table, the
``<table>__crsql_clock`` and ``<table>__crsql_pks`` bookkeeping tables (their
indexes go with them), and the ``crsql_master``, ``crsql_site_id`` and
``crsql_tracked_peers`` metadata tables. ``crsql_changes`` is a virtual view
of the extension and is not stored, so there is nothing to drop for it.

User tables and their rows are not touched, and no table is rebuilt.

Before dropping anything, the machine id the old code derived from
``crsql_site_id()`` is written to ``_metadata['pt.machine_id']`` (unless an
explicit value is already there), so every ID minted afterwards keeps the same
10 machine bits and stays collision-free against existing IDs. The hash is
the one ``db.pt_id`` used: the first two bytes of SHA-256 over the local
(ordinal 0) site id, masked to 10 bits.

Objects are discovered from ``sqlite_master`` rather than listed, and every
DROP uses IF EXISTS, so the migration is a no-op on a fresh database or on
one that never had cr-sqlite. If the site id exists but ``_metadata`` does
not, the table is created (same DDL as schema.py) so the id is not lost.
It works without the extension loaded: the triggers are plain SQL objects
and can be dropped like any others.

Destructive: ``pt db migrate`` takes and verifies a full backup first.
"""

from __future__ import annotations

import hashlib
import sqlite3

_MID_MAX = 1023  # 10 machine bits, as in db.pt_id
_METADATA_KEY = "pt.machine_id"
_METADATA_TABLES = ("crsql_master", "crsql_site_id", "crsql_tracked_peers")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


# Mirrors the _metadata DDL in schema.py ensure_schema (not importable from
# there: it is inline in a large function). Used only when the table is missing.
_METADATA_DDL = """
    CREATE TABLE IF NOT EXISTS _metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
"""


def _preserve_machine_id(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "crsql_site_id"):
        return
    row = conn.execute(
        "SELECT site_id FROM crsql_site_id WHERE ordinal = 0"
    ).fetchone()
    if row is None:
        return
    conn.execute(_METADATA_DDL)
    if conn.execute(
        "SELECT 1 FROM _metadata WHERE key = ?", (_METADATA_KEY,)
    ).fetchone() is not None:
        return
    digest = hashlib.sha256(bytes(row[0])).digest()
    machine_id = ((digest[0] << 8) | digest[1]) & _MID_MAX
    conn.execute(
        "INSERT INTO _metadata (key, value, created_at) "
        "VALUES (?, ?, datetime('now'))",
        (_METADATA_KEY, str(machine_id)),
    )


def up(conn: sqlite3.Connection) -> None:
    _preserve_machine_id(conn)

    triggers = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE '%\\_\\_crsql\\_%' ESCAPE '\\'"
        )
    ]
    for name in triggers:
        conn.execute(f"DROP TRIGGER IF EXISTS {_quote(name)}")

    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE '%\\_\\_crsql\\_%' ESCAPE '\\'"
        )
    ]
    for name in tables:
        conn.execute(f"DROP TABLE IF EXISTS {_quote(name)}")

    for name in _METADATA_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {_quote(name)}")
