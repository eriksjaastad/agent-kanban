"""Integration tests for migration 004 — CRR table NOT NULL column defaults.

Validates that after the migration every NOT NULL non-PK column in all CRR
tables carries a DEFAULT value, row counts are preserved, FK integrity is clean,
triggers are restored, and indexes exist.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# The ten tables migrations 003-008 prepared for cr-sqlite (the sync layer is
# gone, but those migrations still run on restored backups).
CRR_TABLES = frozenset({
    "tasks", "ideas", "task_history", "task_attachments", "projects",
    "project_info", "ai_agents", "service_dependencies", "calendar_events",
    "calendar_event_tasks",
})

_MIGRATION_PATH = REPO / "scripts" / "db" / "migrations" / "004_crr_notnull_defaults.py"
_spec = importlib.util.spec_from_file_location("_m004", _MIGRATION_PATH)
assert _spec and _spec.loader
_m004 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m004)  # type: ignore[union-attr]
up = _m004.up
_patch_defaults = _m004._patch_defaults
_COLUMN_DEFAULTS = _m004._COLUMN_DEFAULTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a DB in the post-003 shape: no AUTOINCREMENT, NOT NULL PKs,
    but NOT NULL non-PK columns still lacking DEFAULT values."""
    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT UNIQUE NOT NULL,
            path TEXT NOT NULL,
            status TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            last_modified TEXT
        );
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE ai_agents (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE service_dependencies (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT NOT NULL,
            service_name TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE project_info (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, key)
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Backlog','To Do','In Progress','Review','Done','Cancelled')),
            project_id TEXT NOT NULL,
            created_at TEXT,
            updated_at TEXT,
            completed_at TEXT,
            blocked_by TEXT,
            parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL DEFAULT 'manual',
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY NOT NULL,
            title TEXT NOT NULL,
            event_date TEXT NOT NULL,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            machine TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE task_history (
            id INTEGER PRIMARY KEY NOT NULL,
            task_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE task_attachments (
            id INTEGER PRIMARY KEY NOT NULL,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL
        );
        CREATE TABLE calendar_event_tasks (
            event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
            task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            link_type TEXT NOT NULL DEFAULT 'related',
            PRIMARY KEY (event_id, task_id)
        );
        CREATE TABLE _delete_permissions (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO _delete_permissions VALUES (1, 0);
        CREATE TABLE delete_audit_log (
            id INTEGER PRIMARY KEY NOT NULL,
            table_name TEXT NOT NULL,
            deleted_id TEXT NOT NULL,
            deleted_data TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            source TEXT DEFAULT 'unknown'
        );
        CREATE TABLE delete_attempt_log (
            id INTEGER PRIMARY KEY NOT NULL,
            table_name TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE INDEX idx_projects_status ON projects(status);
        CREATE INDEX idx_ai_agents_project ON ai_agents(project_id);
        CREATE INDEX idx_service_deps_project ON service_dependencies(project_id);
        CREATE INDEX idx_project_info_project ON project_info(project_id);
        CREATE INDEX idx_tasks_project ON tasks(project_id);
        CREATE INDEX idx_tasks_status ON tasks(status);
        CREATE INDEX idx_tasks_parent ON tasks(parent_id);
        CREATE INDEX idx_cal_date ON calendar_events(event_date);
        CREATE INDEX idx_history_event ON task_history(event_type);
        CREATE INDEX idx_history_project ON task_history(project_id);
        CREATE INDEX idx_history_timestamp ON task_history(timestamp);
        CREATE INDEX idx_attachments_task_id ON task_attachments(task_id);
        CREATE TRIGGER audit_project_delete
        BEFORE DELETE ON projects
        BEGIN
            INSERT INTO delete_audit_log(table_name, deleted_id, deleted_data, deleted_at, source)
            VALUES ('projects', OLD.id, '{}', datetime('now'), 'trigger');
        END;
        CREATE TRIGGER audit_task_delete
        BEFORE DELETE ON tasks
        WHEN (SELECT enabled FROM _delete_permissions WHERE id = 1) = 1
        BEGIN
            INSERT INTO delete_audit_log(table_name, deleted_id, deleted_data, deleted_at, source)
            VALUES ('tasks', OLD.id, '{}', datetime('now'), 'application');
        END;
        CREATE TRIGGER block_task_delete
        BEFORE DELETE ON tasks
        WHEN (SELECT enabled FROM _delete_permissions WHERE id = 1) = 0
        BEGIN
            INSERT INTO delete_attempt_log(table_name, attempted_at, reason)
            VALUES ('tasks', datetime('now'), 'blocked');
            SELECT RAISE(FAIL, 'Task deletes are blocked.');
        END;
    """)

    conn.execute("INSERT INTO projects VALUES ('p1','Proj A','/a','active','desc','2026-01-01',NULL)")
    conn.execute("INSERT INTO ideas VALUES (1,'idea one','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO ai_agents VALUES (1,'p1','agent-x')")
    conn.execute("INSERT INTO service_dependencies VALUES (1,'p1','github')")
    conn.execute("INSERT INTO project_info VALUES (1,'p1','stack','python','2026-01-01')")
    conn.execute("INSERT INTO tasks (id,text,status,project_id,created_at,updated_at,parent_id) VALUES (1,'Task A','Backlog','p1','2026-01-01','2026-01-01',NULL)")
    conn.execute("INSERT INTO tasks (id,text,status,project_id,created_at,updated_at,parent_id) VALUES (2,'Task B','Done','p1','2026-01-02','2026-01-02',1)")
    conn.execute("INSERT INTO calendar_events VALUES (1,'Meeting','2026-06-01','p1',NULL,'active','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO task_history VALUES (1,1,'p1','created','2026-01-01')")
    conn.execute("INSERT INTO task_attachments VALUES (1,1,'file.txt','abc123.txt')")
    conn.execute("INSERT INTO calendar_event_tasks VALUES (1,1,'related')")
    conn.commit()
    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")


def _table_info(conn: sqlite3.Connection, tbl: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
    return [
        {"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt_value": r[4], "pk": r[5]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated(tmp_path: Path):
    conn = _minimal_db(tmp_path)
    before = {tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] for tbl in CRR_TABLES}
    _run_migration(conn)
    return conn, before


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_row_counts_preserved(migrated):
    conn, before = migrated
    for tbl in CRR_TABLES:
        after = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert after == before[tbl], f"{tbl}: row count changed {before[tbl]} → {after}"


def test_all_notnull_columns_have_defaults(migrated):
    conn, _ = migrated
    crr_tables_in_db = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ({})".format(
                ",".join(f"'{t}'" for t in CRR_TABLES)
            )
        ).fetchall()
    }
    for tbl in crr_tables_in_db:
        for col in _table_info(conn, tbl):
            if col["notnull"] == 1 and col["pk"] == 0:
                assert col["dflt_value"] is not None, (
                    f"{tbl}.{col['name']}: NOT NULL column has no DEFAULT after migration"
                )


def test_tasks_status_default_satisfies_check(migrated):
    conn, _ = migrated
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO tasks (id, text, project_id) VALUES (99, 'no-status', 'p1')"
    )
    row = conn.execute("SELECT status FROM tasks WHERE id=99").fetchone()
    assert row[0] == "Backlog", f"tasks.status DEFAULT should be 'Backlog', got {row[0]!r}"


def test_fk_check_clean(migrated, tmp_path: Path):
    conn, _ = migrated
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    check_conn = sqlite3.connect(db_path)
    check_conn.execute("PRAGMA foreign_keys = ON")
    violations = check_conn.execute("PRAGMA foreign_key_check").fetchall()
    check_conn.close()
    assert violations == [], f"FK violations after migration: {violations}"


def test_integrity_check_ok(migrated):
    conn, _ = migrated
    result = conn.execute("PRAGMA integrity_check").fetchone()
    assert result[0] == "ok", f"integrity_check failed: {result[0]}"


def test_triggers_restored(migrated):
    conn, _ = migrated
    trigger_names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "audit_task_delete" in trigger_names
    assert "block_task_delete" in trigger_names
    assert "audit_project_delete" in trigger_names


def test_expected_indexes_present(migrated):
    conn, _ = migrated
    existing = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    required = {
        "idx_tasks_project", "idx_tasks_status", "idx_tasks_parent",
        "idx_ai_agents_project", "idx_service_deps_project",
        "idx_project_info_project", "idx_projects_status",
        "idx_history_event", "idx_history_project", "idx_history_timestamp",
        "idx_attachments_task_id", "idx_cal_date",
    }
    assert not (required - existing), f"Indexes missing: {required - existing}"


def test_data_values_preserved(migrated):
    conn, _ = migrated
    assert conn.execute("SELECT text FROM ideas WHERE id=1").fetchone()[0] == "idea one"
    task = conn.execute("SELECT text, parent_id FROM tasks WHERE id=2").fetchone()
    assert task[0] == "Task B" and task[1] == 1


def test_idempotent(tmp_path: Path):
    conn = _minimal_db(tmp_path)
    conn.execute("BEGIN"); up(conn); conn.execute("COMMIT")
    conn.execute("BEGIN"); up(conn); conn.execute("COMMIT")
    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Unit tests for _patch_defaults
# ---------------------------------------------------------------------------


def test_patch_adds_default_to_text_notnull():
    sql = "CREATE TABLE ideas (id INTEGER PRIMARY KEY NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL)"
    result = _patch_defaults(sql, "ideas")
    assert "text TEXT NOT NULL DEFAULT ''" in result
    assert "created_at TEXT NOT NULL DEFAULT ''" in result


def test_patch_does_not_double_add_default():
    sql = "CREATE TABLE ideas (id INTEGER PRIMARY KEY NOT NULL, text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)"
    result = _patch_defaults(sql, "ideas")
    assert result.count("DEFAULT ''") == 2  # text already had one; created_at gets one
    assert "DEFAULT '' DEFAULT" not in result


def test_patch_tasks_status_uses_backlog():
    sql = "CREATE TABLE tasks (id INTEGER PRIMARY KEY NOT NULL, status TEXT NOT NULL CHECK(status IN ('Backlog','Done')))"
    result = _patch_defaults(sql, "tasks")
    assert "DEFAULT 'Backlog'" in result


def test_patch_integer_fk_gets_zero():
    sql = "CREATE TABLE task_history (id INTEGER PRIMARY KEY NOT NULL, task_id INTEGER NOT NULL, event_type TEXT NOT NULL)"
    result = _patch_defaults(sql, "task_history")
    assert "task_id INTEGER NOT NULL DEFAULT 0" in result
    assert "event_type TEXT NOT NULL DEFAULT ''" in result


def test_patch_skips_pk_column():
    sql = "CREATE TABLE ideas (id INTEGER PRIMARY KEY NOT NULL, text TEXT NOT NULL)"
    result = _patch_defaults(sql, "ideas")
    # id is a PK — the regex won't add a DEFAULT because "id" is not in _COLUMN_DEFAULTS
    assert "id INTEGER PRIMARY KEY NOT NULL DEFAULT" not in result
