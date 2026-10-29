"""Integration tests for migration 005 — strip FK constraint declarations from CRR tables."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

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

_MIGRATION_PATH = REPO / "scripts" / "db" / "migrations" / "005_crr_drop_fk_constraints.py"
_spec = importlib.util.spec_from_file_location("_m005", _MIGRATION_PATH)
assert _spec and _spec.loader
_m005 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m005)  # type: ignore[union-attr]
up = _m005.up
_strip_fk_constraints = _m005._strip_fk_constraints


# ---------------------------------------------------------------------------
# Minimal DB (post-003+004 shape: NOT NULL on PKs, defaults on all NOT NULL cols)
# ---------------------------------------------------------------------------


def _minimal_db(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY NOT NULL,
            name TEXT UNIQUE NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            description TEXT,
            created_at TEXT NOT NULL DEFAULT '',
            last_modified TEXT
        );
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE ai_agents (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            agent_name TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE service_dependencies (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            service_name TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE project_info (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT,
            key TEXT NOT NULL DEFAULT '',
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(project_id, key)
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Backlog'
                CHECK(status IN ('Backlog','To Do','In Progress','Review','Done','Cancelled')),
            project_id TEXT NOT NULL DEFAULT '',
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
            title TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            machine TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE task_history (
            id INTEGER PRIMARY KEY NOT NULL,
            task_id INTEGER NOT NULL DEFAULT 0,
            project_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE task_attachments (
            id INTEGER PRIMARY KEY NOT NULL,
            task_id INTEGER NOT NULL DEFAULT 0 REFERENCES tasks(id) ON DELETE CASCADE,
            filename TEXT NOT NULL DEFAULT '',
            stored_name TEXT NOT NULL DEFAULT ''
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
    conn.execute("INSERT INTO tasks (id,text,status,project_id) VALUES (1,'Task A','Backlog','p1')")
    conn.execute("INSERT INTO tasks (id,text,status,project_id,parent_id) VALUES (2,'Task B','Done','p1',1)")
    conn.execute("INSERT INTO calendar_events (id,title,event_date,project_id) VALUES (1,'Meeting','2026-06-01','p1')")
    conn.execute("INSERT INTO task_history (id,task_id,project_id,event_type,timestamp) VALUES (1,1,'p1','created','2026-01-01')")
    conn.execute("INSERT INTO task_attachments (id,task_id,filename,stored_name) VALUES (1,1,'file.txt','abc.txt')")
    conn.execute("INSERT INTO calendar_event_tasks VALUES (1,1,'related')")
    conn.commit()
    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")


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


def test_no_fk_constraints_in_crr_ddl(migrated):
    conn, _ = migrated
    crr_in_db = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ({})".format(
                ",".join(f"'{t}'" for t in CRR_TABLES)
            )
        ).fetchall()
    }
    for tbl in crr_in_db:
        fks = conn.execute(f"PRAGMA foreign_key_list({tbl})").fetchall()
        assert fks == [], f"{tbl}: FK constraints still present after migration: {fks}"


def test_fk_columns_still_exist(migrated):
    """project_id, task_id, parent_id columns must survive — only constraints removed."""
    conn, _ = migrated
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "project_id" in cols
    assert "parent_id" in cols
    cols_hist = {r[1] for r in conn.execute("PRAGMA table_info(task_history)").fetchall()}
    assert "task_id" in cols_hist
    assert "project_id" in cols_hist


def test_data_values_preserved(migrated):
    conn, _ = migrated
    assert conn.execute("SELECT text FROM ideas WHERE id=1").fetchone()[0] == "idea one"
    task = conn.execute("SELECT text, parent_id FROM tasks WHERE id=2").fetchone()
    assert task[0] == "Task B" and task[1] == 1


def test_integrity_check_ok(migrated):
    conn, _ = migrated
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_triggers_restored(migrated):
    conn, _ = migrated
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
    assert {"audit_task_delete", "block_task_delete", "audit_project_delete"} <= names


def test_expected_indexes_present(migrated):
    conn, _ = migrated
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    required = {
        "idx_tasks_project", "idx_tasks_status", "idx_tasks_parent",
        "idx_ai_agents_project", "idx_service_deps_project",
        "idx_project_info_project", "idx_projects_status",
        "idx_history_event", "idx_history_project", "idx_history_timestamp",
        "idx_attachments_task_id", "idx_cal_date",
    }
    assert not (required - existing), f"Missing: {required - existing}"


def test_idempotent(tmp_path: Path):
    conn = _minimal_db(tmp_path)
    _run_migration(conn)
    _run_migration(conn)
    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1
    fks = conn.execute("PRAGMA foreign_key_list(tasks)").fetchall()
    assert fks == []


# ---------------------------------------------------------------------------
# Unit tests for _strip_fk_constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "absent", "present"),
    [
        pytest.param(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, project_id TEXT, "
            "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE)",
            ["FOREIGN KEY", "REFERENCES"], ["project_id TEXT"],
            id="standalone_fk",
        ),
        pytest.param(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, "
            "task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE, name TEXT)",
            ["REFERENCES"], ["task_id INTEGER", "name TEXT"],
            id="inline_references",
        ),
        pytest.param(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, "
            "project_id TEXT REFERENCES projects(id) ON DELETE SET NULL)",
            ["REFERENCES"], [],
            id="set_null_action",
        ),
        pytest.param(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, task_id INTEGER, project_id TEXT, "
            "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE, "
            "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE)",
            ["FOREIGN KEY"], ["task_id INTEGER", "project_id TEXT"],
            id="multiple_standalone_fks",
        ),
    ],
)
def test_strips_fk_constraints(sql, absent, present):
    result = _strip_fk_constraints(sql)
    for text in absent:
        assert text not in result.upper()
    for text in present:
        assert text in result


def test_non_crr_references_untouched():
    """Stripping is applied only to the DDL passed in — no global side effects."""
    sql = "CREATE TABLE other (id INTEGER PRIMARY KEY, val TEXT)"
    assert _strip_fk_constraints(sql) == sql


def test_preserves_check_constraints():
    sql = ("CREATE TABLE t (id INTEGER PRIMARY KEY, "
           "status TEXT NOT NULL DEFAULT 'Backlog' CHECK(status IN ('Backlog','Done')), "
           "project_id TEXT, FOREIGN KEY (project_id) REFERENCES projects(id))")
    result = _strip_fk_constraints(sql)
    assert "CHECK" in result
    assert "FOREIGN KEY" not in result.upper()
