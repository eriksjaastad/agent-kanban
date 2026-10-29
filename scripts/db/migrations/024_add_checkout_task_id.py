"""Add project_checkouts.task_id for #8626 (remind to check a project back in).

A checkout taken for a card records that card's task PK; an owned checkout
(a project kept on one machine on purpose) leaves it NULL, and only checkouts
with a card get check-in reminders. Every checkout that existed before this
migration is therefore treated as owned.

Additive and idempotent: a fresh database already has the column from the
generated schema.sql, so it is added only when missing.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(project_checkouts)")}
    if "task_id" not in columns:
        conn.execute("ALTER TABLE project_checkouts ADD COLUMN task_id INTEGER")
