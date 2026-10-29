"""Add the outreach_contacts table for #7641.

A private, laptop-only list of people the owner wants to reach out to from the
Morning page. Names are personal data, so this table deliberately has no sync
contract: it is LOCAL_ONLY and only the outreach API endpoints return it.

Classification: LOCAL_ONLY. Contact rows never leave the laptop; the Mini has
no use for them and replicating private names would widen their exposure for
no benefit. The same DDL lives in schema.py for fresh databases; CREATE IF
NOT EXISTS makes this migration safe after either creation path. Rows are
never hard-deleted — application delete is a soft delete (deleted_at), and
"replied" rows stay in the table as finished lifecycle evidence.
"""

from __future__ import annotations

import sqlite3

CRR_TABLES: frozenset[str] = frozenset()


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outreach_contacts (
            id           INTEGER PRIMARY KEY NOT NULL,
            name         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            contacted_at TEXT,
            replied_at   TEXT,
            deleted_at   TEXT,
            updated_at   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_outreach_contacts_deleted_replied
        ON outreach_contacts(deleted_at, replied_at)
    """)
