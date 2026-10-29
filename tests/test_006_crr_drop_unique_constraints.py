"""Integration tests for migration 006 — strip non-PK UNIQUE constraints."""

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

_MIGRATION_PATH = REPO / "scripts" / "db" / "migrations" / "006_crr_drop_unique_constraints.py"
_spec = importlib.util.spec_from_file_location("_m006", _MIGRATION_PATH)
assert _spec and _spec.loader
_m006 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m006)  # type: ignore[union-attr]
up = _m006.up


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
            agent_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE service_dependencies (
            id INTEGER PRIMARY KEY NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            service_name TEXT NOT NULL DEFAULT ''
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
            status TEXT NOT NULL DEFAULT 'Backlog',
            project_id TEXT NOT NULL DEFAULT '',
            created_at TEXT,
            updated_at TEXT,
            completed_at TEXT,
            parent_id INTEGER,
            task_type TEXT NOT NULL DEFAULT 'manual'
        );
        CREATE TABLE calendar_events (
            id INTEGER PRIMARY KEY NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            project_id TEXT,
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
            timestamp TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE task_attachments (
            id INTEGER PRIMARY KEY NOT NULL,
            task_id INTEGER NOT NULL DEFAULT 0,
            filename TEXT NOT NULL DEFAULT '',
            stored_name TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE calendar_event_tasks (
            event_id INTEGER NOT NULL,
            task_id  INTEGER NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'related',
            PRIMARY KEY (event_id, task_id)
        );
        CREATE INDEX idx_projects_last_modified ON projects(last_modified DESC);
        CREATE INDEX idx_projects_status ON projects(status);
        CREATE INDEX idx_project_info_project ON project_info(project_id);
    """)
    conn.execute("INSERT INTO projects VALUES ('p1','Proj A','/a','active','desc','2026-01-01',NULL)")
    conn.execute("INSERT INTO project_info VALUES (1,'p1','stack','python','2026-01-01')")
    conn.execute("INSERT INTO ideas VALUES (1,'idea one','2026-01-01','2026-01-01')")
    conn.commit()
    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")


def _non_pk_unique_indexes(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1]
        for row in conn.execute(f"PRAGMA index_list('{table}')").fetchall()
        if row[2] and row[3] != "pk"
    ]


@pytest.fixture
def migrated(tmp_path: Path):
    conn = _minimal_db(tmp_path)
    before = {
        tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        for tbl in {"projects", "project_info"}
    }
    _run_migration(conn)
    return conn, before


def test_row_counts_preserved(migrated):
    conn, before = migrated
    for tbl, expected in before.items():
        after = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert after == expected, f"{tbl}: row count changed {expected} -> {after}"


def test_non_pk_unique_indexes_removed(migrated):
    conn, _ = migrated
    assert _non_pk_unique_indexes(conn, "projects") == []
    assert _non_pk_unique_indexes(conn, "project_info") == []


def test_expected_lookup_indexes_present(migrated):
    conn, _ = migrated
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    required = {
        "idx_projects_last_modified",
        "idx_projects_name",
        "idx_projects_status",
        "idx_project_info_project",
        "idx_project_info_scope_key",
    }
    assert not (required - existing), f"Missing: {required - existing}"


def test_integrity_check_ok(migrated):
    conn, _ = migrated
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_idempotent(tmp_path: Path):
    conn = _minimal_db(tmp_path)
    before = {
        tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        for tbl in ("projects", "project_info")
    }
    _run_migration(conn)
    _run_migration(conn)
    assert _non_pk_unique_indexes(conn, "projects") == []
    assert _non_pk_unique_indexes(conn, "project_info") == []
    for tbl, expected in before.items():
        after = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert after == expected, f"{tbl}: idempotent double-run changed row count {expected} → {after}"
