"""Restore the audit_project_delete trigger (#8093, plan 4.5).

Migration 006 rebuilt `projects` (DROP + RENAME) without saving and restoring
its triggers, and dropping a table drops its triggers. Since then no project
deletion has been written to delete_audit_log. Migrations 003, 004, 005 and
007 did preserve their triggers; 006 is the one that lost this.

The body is the one the old schema code created. IF NOT EXISTS makes this a
no-op where the trigger survived.
"""

from __future__ import annotations

import sqlite3

DDL = """
    CREATE TRIGGER IF NOT EXISTS audit_project_delete
    BEFORE DELETE ON projects
    FOR EACH ROW
    BEGIN
        INSERT INTO delete_audit_log (table_name, deleted_id, deleted_data, deleted_at, source)
        VALUES (
            'projects',
            OLD.id,
            json_object(
                'id', OLD.id,
                'name', OLD.name,
                'path', OLD.path,
                'status', OLD.status,
                'description', OLD.description
            ),
            datetime('now'),
            'trigger'
        );
    END
"""


def up(conn: sqlite3.Connection) -> None:
    conn.execute(DDL)
