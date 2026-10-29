"""Tests for migration 007 — add DEFAULT '' to tasks/calendar_events timestamps."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_MIGRATION_PATH = REPO / "scripts" / "db" / "migrations" / "007_crr_fix_missing_defaults.py"
_spec = importlib.util.spec_from_file_location("_m007", _MIGRATION_PATH)
assert _spec and _spec.loader
_m007 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m007)  # type: ignore[union-attr]
up = _m007.up


def _db_with_broken_defaults(tmp_path: Path) -> sqlite3.Connection:
    """tasks and calendar_events with NOT NULL but no DEFAULT — the Mini-failure schema."""
    conn = sqlite3.connect(str(tmp_path / "tracker.db"))
    conn.executescript("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Backlog',
            project_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            parent_id INTEGER,
            blocked_by TEXT
        );
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            machine TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks (id, text, status, project_id, created_at, updated_at)
            VALUES (1, 'task one', 'Backlog', 'p1', '2026-01-01', '2026-01-01');
        INSERT INTO calendar_events (id, title, event_date, created_at, updated_at)
            VALUES (1, 'event one', '2026-06-01', '2026-01-01', '2026-01-01');
    """)
    conn.commit()
    return conn


def _db_already_compliant(tmp_path: Path) -> sqlite3.Connection:
    """tasks and calendar_events already have DEFAULT '' — migration should no-op."""
    conn = sqlite3.connect(str(tmp_path / "tracker.db"))
    conn.executescript("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT,
            parent_id INTEGER,
            blocked_by TEXT
        );
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            machine TEXT,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO tasks (id, text, created_at, updated_at)
            VALUES (1, 'task one', '2026-01-01', '2026-01-01');
        INSERT INTO calendar_events (id, title, event_date, created_at, updated_at)
            VALUES (1, 'event', '2026-06-01', '2026-01-01', '2026-01-01');
    """)
    conn.commit()
    return conn


def _get_col_defaults(conn: sqlite3.Connection, tbl: str) -> dict[str, str | None]:
    return {r[1]: r[4] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}


def test_defaults_added_to_broken_schema(tmp_path: Path) -> None:
    conn = _db_with_broken_defaults(tmp_path)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    defaults = _get_col_defaults(conn, "tasks")
    assert defaults["created_at"] == "''", f"tasks.created_at DEFAULT: {defaults['created_at']!r}"
    assert defaults["updated_at"] == "''", f"tasks.updated_at DEFAULT: {defaults['updated_at']!r}"

    defaults = _get_col_defaults(conn, "calendar_events")
    assert defaults["created_at"] == "''", f"calendar_events.created_at DEFAULT: {defaults['created_at']!r}"
    assert defaults["updated_at"] == "''", f"calendar_events.updated_at DEFAULT: {defaults['updated_at']!r}"

    # Rows and integrity survive the rebuild.
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 1

    task_row = conn.execute("SELECT text, created_at FROM tasks WHERE id=1").fetchone()
    assert task_row[0] == "task one"
    assert task_row[1] == "2026-01-01"

    cal_row = conn.execute("SELECT title, event_date FROM calendar_events WHERE id=1").fetchone()
    assert cal_row[0] == "event one"
    assert cal_row[1] == "2026-06-01"

    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_defaults_added_when_table_name_quoted_in_schema(tmp_path: Path) -> None:
    """sqlite_master may store CREATE TABLE "tasks" (quoted) — regex must handle it.

    This is the production case on the Mac Mini where the tasks table was created
    with a quoted identifier. The rename regex previously used \\b which fails after
    a closing double-quote, producing ""tasks_new"" (unrecognized token).
    """
    conn = sqlite3.connect(str(tmp_path / "tracker.db"))
    conn.executescript("""
        CREATE TABLE "tasks" (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Backlog',
            project_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            parent_id INTEGER,
            blocked_by TEXT
        );
        INSERT INTO "tasks" (id, text, status, project_id, created_at, updated_at)
            VALUES (1, 'task one', 'Backlog', 'p1', '2026-01-01', '2026-01-01');
    """)
    conn.commit()

    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    defaults = _get_col_defaults(conn, "tasks")
    assert defaults["created_at"] == "''", f"tasks.created_at DEFAULT: {defaults['created_at']!r}"
    assert defaults["updated_at"] == "''", f"tasks.updated_at DEFAULT: {defaults['updated_at']!r}"
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_triggers_restored_after_rebuild(tmp_path: Path) -> None:
    """audit_task_delete and block_task_delete survive the tasks table rebuild.

    Also verifies that audit_project_delete (bound to projects, which is NOT
    rebuilt) does not cause a 'trigger already exists' collision in the restore
    loop — this is the production scenario on any machine that has run ensure_schema.
    """
    conn = _db_with_broken_defaults(tmp_path)
    # Add a projects table so audit_project_delete is a valid trigger
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS _delete_permissions (id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS delete_audit_log (
            id INTEGER PRIMARY KEY, table_name TEXT, deleted_id INTEGER,
            deleted_data TEXT, deleted_at TEXT, source TEXT
        );
        CREATE TABLE IF NOT EXISTS delete_attempt_log (
            id INTEGER PRIMARY KEY, table_name TEXT, attempted_at TEXT, reason TEXT
        );
        INSERT INTO _delete_permissions VALUES (1, 0);
        CREATE TRIGGER audit_task_delete
        BEFORE DELETE ON tasks
        WHEN (SELECT enabled FROM _delete_permissions WHERE id = 1) = 1
        BEGIN
            INSERT INTO delete_audit_log (table_name, deleted_id, deleted_data, deleted_at, source)
            VALUES ('tasks', OLD.id, '{}', datetime('now'), 'application');
        END;
        CREATE TRIGGER block_task_delete
        BEFORE DELETE ON tasks
        WHEN (SELECT enabled FROM _delete_permissions WHERE id = 1) = 0
        BEGIN
            INSERT INTO delete_attempt_log (table_name, attempted_at, reason)
            VALUES ('tasks', datetime('now'), 'blocked');
            SELECT RAISE(FAIL, 'blocked');
        END;
        CREATE TRIGGER audit_project_delete
        BEFORE DELETE ON projects
        BEGIN
            INSERT INTO delete_audit_log (table_name, deleted_id, deleted_data, deleted_at, source)
            VALUES ('projects', OLD.id, '{}', datetime('now'), 'application');
        END;
    """)

    # Must not raise — audit_project_delete already exists and projects is not dropped
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "audit_task_delete" in triggers, "audit_task_delete trigger was not restored"
    assert "block_task_delete" in triggers, "block_task_delete trigger was not restored"
    assert "audit_project_delete" in triggers, "audit_project_delete trigger disappeared"


def test_no_op_when_already_compliant(tmp_path: Path) -> None:
    """Migration must skip entirely when columns already have DEFAULT values."""
    import io, contextlib

    conn = _db_already_compliant(tmp_path)
    before_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    before_cal = conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]

    stderr_buf = io.StringIO()
    conn.execute("BEGIN")
    with contextlib.redirect_stderr(stderr_buf):
        up(conn)
    conn.execute("COMMIT")

    assert "nothing to do" in stderr_buf.getvalue(), (
        f"expected no-op message, got: {stderr_buf.getvalue()!r}"
    )
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before_tasks
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == before_cal


def test_raises_if_column_type_not_text(tmp_path: Path) -> None:
    """If created_at is DATETIME (not TEXT), _patch_sql silently no-ops.
    The post-rebuild verification must catch this and raise RuntimeError."""
    conn = sqlite3.connect(str(tmp_path / "tracker.db"))
    conn.executescript("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Backlog',
            project_id TEXT NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO tasks (id, text, status, project_id, created_at, updated_at)
            VALUES (1, 'task one', 'Backlog', 'p1', '2026-01-01', '2026-01-01');
    """)
    conn.commit()

    import pytest
    conn.execute("BEGIN")
    with pytest.raises(RuntimeError, match="Column type may not be TEXT"):
        up(conn)
    conn.execute("ROLLBACK")


def test_idempotent(tmp_path: Path) -> None:
    """Running up() twice on a broken schema must be safe — second run is a no-op."""
    conn = _db_with_broken_defaults(tmp_path)

    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    defaults = _get_col_defaults(conn, "tasks")
    assert defaults["created_at"] == "''"
