"""Integration tests for migration 003 — CRR table PK NOT NULL + no AUTOINCREMENT.

Validates the table-rebuild migration on a throwaway SQLite database seeded
with representative rows across all 10 CRR tables. After migration:

- Row counts are preserved.
- Every CRR table PK column shows notnull=1 in PRAGMA table_info.
- No CRR table DDL contains AUTOINCREMENT.
- PRAGMA foreign_key_check returns no violations.
- PRAGMA integrity_check returns "ok".
- All expected indexes exist.
- The audit triggers (audit_task_delete, block_task_delete,
  audit_project_delete) are present and fire correctly.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

import importlib.util

import pytest

# Path setup — two levels up from tests/ is project root.
REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# The ten tables migrations 003-008 prepared for cr-sqlite (the sync layer is
# gone, but those migrations still run on restored backups).
CRR_TABLES = frozenset({
    "tasks", "ideas", "task_history", "task_attachments", "projects",
    "project_info", "ai_agents", "service_dependencies", "calendar_events",
    "calendar_event_tasks",
})

# Migration filename starts with a digit — can't use regular import syntax.
_MIGRATION_PATH = REPO / "scripts" / "db" / "migrations" / "003_crr_pk_not_null.py"
_spec = importlib.util.spec_from_file_location("_m003", _MIGRATION_PATH)
assert _spec and _spec.loader
_m003 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m003)  # type: ignore[union-attr]
up = _m003.up
_make_new_table_sql = _m003._make_new_table_sql
_patch_pk_ddl = _m003._patch_pk_ddl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_db(tmp_path: Path) -> sqlite3.Connection:
    """Create a minimal tracker.db with all 10 CRR tables + support tables."""
    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            path TEXT NOT NULL,
            status TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL,
            last_modified TEXT
        );
        CREATE TABLE ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE ai_agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE service_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            service_name TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE project_info (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, key)
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            status TEXT NOT NULL,
            project_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            blocked_by INTEGER,
            parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_date TEXT NOT NULL,
            project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
            machine TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE TABLE task_attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            deleted_id TEXT NOT NULL,
            deleted_data TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            source TEXT DEFAULT 'unknown'
        );
        CREATE TABLE delete_attempt_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        -- Indexes (like the live DB)
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
        -- Triggers
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

    # Seed rows
    conn.execute(
        "INSERT INTO projects VALUES ('p1','Proj A','/a','active','desc','2026-01-01',NULL)"
    )
    conn.execute("INSERT INTO ideas VALUES (1,'idea one','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO ideas VALUES (2,'idea two','2026-01-02','2026-01-02')")
    conn.execute("INSERT INTO ai_agents VALUES (1,'p1','agent-x')")
    conn.execute("INSERT INTO service_dependencies VALUES (1,'p1','github')")
    conn.execute("INSERT INTO project_info VALUES (1,'p1','stack','python','2026-01-01')")
    conn.execute("INSERT INTO tasks VALUES (1,'Task A','Backlog','p1','2026-01-01','2026-01-01',NULL,NULL,NULL)")
    conn.execute("INSERT INTO tasks VALUES (2,'Task B','Done','p1','2026-01-02','2026-01-02',NULL,NULL,1)")
    conn.execute("INSERT INTO calendar_events VALUES (1,'Meeting','2026-06-01','p1',NULL,'active','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO task_history VALUES (1,1,'p1','created','2026-01-01')")
    conn.execute("INSERT INTO task_attachments VALUES (1,1)")
    conn.execute("INSERT INTO calendar_event_tasks VALUES (1,1,'related')")
    conn.commit()
    return conn


def _before_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        for tbl in CRR_TABLES
    }


def _run_migration(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")


def _pragma_table_info(conn: sqlite3.Connection, tbl: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
    return [
        {"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt": r[4], "pk": r[5]}
        for r in rows
    ]


def _pk_cols(conn: sqlite3.Connection, tbl: str) -> list[dict[str, Any]]:
    return [c for c in _pragma_table_info(conn, tbl) if c["pk"] > 0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated(tmp_path: Path):
    conn = _minimal_db(tmp_path)
    before = _before_counts(conn)
    _run_migration(conn)
    return conn, before


def test_migrated_data_intact(migrated):
    """Row counts and values survive; FK check and integrity check are clean."""
    conn, before = migrated
    for tbl in CRR_TABLES:
        after = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert after == before[tbl], f"{tbl}: row count changed {before[tbl]} → {after}"

    row = conn.execute("SELECT text FROM ideas WHERE id=1").fetchone()
    assert row is not None and row[0] == "idea one"
    row2 = conn.execute("SELECT text FROM ideas WHERE id=2").fetchone()
    assert row2 is not None and row2[0] == "idea two"
    task = conn.execute("SELECT text, parent_id FROM tasks WHERE id=2").fetchone()
    assert task is not None
    assert task[0] == "Task B"
    assert task[1] == 1  # self-referential FK preserved

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    # Open a fresh connection with FK on to run the check outside the migration txn.
    check_conn = sqlite3.connect(db_path)
    check_conn.execute("PRAGMA foreign_keys = ON")
    violations = check_conn.execute("PRAGMA foreign_key_check").fetchall()
    check_conn.close()
    assert violations == [], f"FK violations after migration: {violations}"

    result = conn.execute("PRAGMA integrity_check").fetchone()
    assert result[0] == "ok", f"integrity_check failed: {result[0]}"


def test_pk_columns_have_explicit_not_null(migrated):
    conn, _ = migrated
    for tbl in CRR_TABLES:
        for col in _pk_cols(conn, tbl):
            # PRAGMA table_info notnull=1 means explicit NOT NULL in DDL
            assert col["notnull"] == 1, (
                f"{tbl}.{col['name']}: PK column notnull={col['notnull']}, expected 1"
            )


def test_no_autoincrement_in_crr_ddl(migrated):
    conn, _ = migrated
    for tbl in CRR_TABLES:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
        assert row is not None
        assert "AUTOINCREMENT" not in row[0].upper(), (
            f"{tbl}: AUTOINCREMENT still present in DDL after migration"
        )


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
    missing = required - existing
    assert not missing, f"Indexes missing after migration: {missing}"


def test_task_triggers_restored(migrated):
    conn, _ = migrated
    trigger_names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "audit_task_delete" in trigger_names
    assert "block_task_delete" in trigger_names
    assert "audit_project_delete" in trigger_names


def test_audit_task_delete_trigger_fires(migrated):
    conn, _ = migrated
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("UPDATE _delete_permissions SET enabled=1 WHERE id=1")
    conn.execute("DELETE FROM task_attachments WHERE task_id=1")
    conn.execute("DELETE FROM task_history WHERE task_id=1")
    conn.execute("DELETE FROM calendar_event_tasks WHERE task_id=1")
    conn.execute("DELETE FROM tasks WHERE id=1")
    count = conn.execute(
        "SELECT COUNT(*) FROM delete_audit_log WHERE table_name='tasks'"
    ).fetchone()[0]
    assert count >= 1, "audit_task_delete trigger did not fire"
    conn.execute("UPDATE _delete_permissions SET enabled=0 WHERE id=1")


def test_block_task_delete_trigger_fires(migrated):
    conn, _ = migrated
    with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError)):
        conn.execute("DELETE FROM tasks WHERE id=2")


def test_self_referential_fk_valid(migrated):
    conn, _ = migrated
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO tasks (id, text, status, project_id, created_at, updated_at, parent_id)"
        " VALUES (3,'Child','Backlog','p1','2026-01-03','2026-01-03',2)"
    )
    conn.commit()
    row = conn.execute("SELECT parent_id FROM tasks WHERE id=3").fetchone()
    assert row[0] == 2


# ---------------------------------------------------------------------------
# Unit tests for _patch_pk_ddl and _make_new_table_sql helpers
# ---------------------------------------------------------------------------


def test_patch_pk_removes_autoincrement():
    sql = "CREATE TABLE foo (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
    result = _patch_pk_ddl(sql)
    assert "AUTOINCREMENT" not in result.upper()
    assert "INTEGER PRIMARY KEY NOT NULL" in result


def test_patch_pk_adds_not_null_to_text_pk():
    sql = "CREATE TABLE foo (id TEXT PRIMARY KEY, name TEXT)"
    result = _patch_pk_ddl(sql)
    assert "TEXT PRIMARY KEY NOT NULL" in result


def test_patch_pk_idempotent_on_already_correct():
    sql = "CREATE TABLE foo (id INTEGER PRIMARY KEY NOT NULL, name TEXT)"
    result = _patch_pk_ddl(sql)
    assert result == sql
    assert "NOT NULL NOT NULL" not in result


def test_make_new_table_sql_renames_table():
    sql = 'CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)'
    result = _make_new_table_sql(sql, "tasks")
    assert '"tasks_new"' in result
    assert "AUTOINCREMENT" not in result.upper()
    assert "INTEGER PRIMARY KEY NOT NULL" in result


def test_make_new_table_sql_handles_quoted_name():
    sql = 'CREATE TABLE "tasks" (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)'
    result = _make_new_table_sql(sql, "tasks")
    assert '"tasks_new"' in result
    assert '"tasks"' not in result


def test_idempotent_on_already_migrated_db(tmp_path: Path):
    """Running up() twice must not fail (IF NOT EXISTS guards the _new tables)."""
    conn = _minimal_db(tmp_path)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")
    # Second run: tables already rebuilt, no _new tables, should be a no-op
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")
    # Row counts still intact
    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 2
