"""create_database: one creation path (schema.sql), one ledger (schema_migrations)."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.db.schema_dump as schema_dump_module
from scripts.db import schema
from scripts.db.migration_runner import (
    apply_all,
    discover_migrations,
    unapplied_migrations,
)
from scripts.db.schema_dump import main as schema_dump


def _objects(path: Path) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()


def test_schema_sql_is_current_with_the_migrations():
    """Fails when a migration lands without `make schema`."""
    assert schema_dump(["--check"]) == 0


def test_a_hand_edited_schema_sql_is_refused(tmp_path, monkeypatch):
    """schema.sql changes only through `make schema`; an edit must not pass silently."""
    edited = tmp_path / "schema.sql"
    edited.write_text(schema.SCHEMA_SQL.read_text().replace("DEFAULT 'user'", "DEFAULT 'agent'", 1))
    monkeypatch.setattr(schema_dump_module, "SCHEMA_SQL", edited)
    before = edited.read_text()
    assert schema_dump(["--check"]) == 1
    assert schema_dump(["--write"]) == 1
    assert edited.read_text() == before


@pytest.mark.parametrize(("text", "error"), [
    ("CREATE TABLE t (id INTEGER);\n", "no '-- Migrations: through NNN' header"),
    ("-- Migrations: through 999\nCREATE TABLE t (id INTEGER);\n", "that are not in"),
    ("-- Migrations: through 002\nCREATE TABLE t (id INTEGER\n", "incomplete statement"),
])
def test_a_malformed_schema_sql_is_refused_before_anything_is_written(tmp_path, monkeypatch, text, error):
    broken = tmp_path / "schema.sql"
    broken.write_text(text)
    monkeypatch.setattr(schema, "SCHEMA_SQL", broken)
    db_path = tmp_path / "tracker.db"
    with pytest.raises(RuntimeError, match=error):
        schema.create_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_fresh_database_is_marked_for_exactly_the_migrations_schema_sql_contains(tmp_path):
    db_path = tmp_path / "tracker.db"
    schema.create_database(db_path)
    version = schema._schema_sql_version(schema.SCHEMA_SQL.read_text())
    migrations = discover_migrations(schema.MIGRATIONS_DIR, verbose=False)
    with closing(sqlite3.connect(db_path)) as conn:
        ledger = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert ledger == [m.version for m in migrations if m.version <= version]
        assert unapplied_migrations(conn, migrations) == []
        # The ledgers nothing reads are not created any more.
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"schema_version", "schema_migration_announcements"} & tables


def test_fresh_database_has_its_seed_row(tmp_path):
    """Without it every task delete is blocked (block_task_delete reads it)."""
    db_path = tmp_path / "tracker.db"
    schema.create_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT id, enabled FROM _delete_permissions").fetchall() == [(1, 0)]


def test_existing_database_is_never_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("PT_ALLOW_FRESH_DB", "1")
    db_path = tmp_path / "tracker.db"
    schema.create_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("INSERT INTO projects (id, name, path, status) VALUES ('p', 'P', '/p', 'active')")
        conn.execute("DROP INDEX idx_projects_name")  # drift a real database might carry
        conn.commit()
    before = _objects(db_path)
    schema.create_database(db_path)
    assert _objects(db_path) == before
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_database_without_the_ledger_is_refused_and_left_alone(tmp_path):
    db_path = tmp_path / "old.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, text TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('1', 'keep me')")
        conn.commit()
    before = _objects(db_path)
    with pytest.raises(schema.SafetyError, match="no schema_migrations ledger"):
        schema.create_database(db_path)
    assert _objects(db_path) == before


def test_a_failing_statement_leaves_the_new_database_empty(tmp_path, monkeypatch):
    broken = tmp_path / "schema.sql"
    broken.write_text(
        "-- Migrations: through 002\n"
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);\n"
        "CREATE TABLE first (id INTEGER);\n"
        "CREATE TABLE first (id INTEGER);\n"
    )
    monkeypatch.setattr(schema, "SCHEMA_SQL", broken)
    db_path = tmp_path / "tracker.db"
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        schema.create_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_two_processes_creating_the_same_new_database_both_succeed(tmp_path, monkeypatch):
    """The second creator finds the tables under the write lock and stands down."""
    monkeypatch.setenv("PT_ALLOW_FRESH_DB", "1")
    db_path = tmp_path / "tracker.db"
    real = schema._statements

    def other_process_builds_first(script):
        monkeypatch.setattr(schema, "_statements", real)
        schema.create_database(db_path)  # wins the race between our check and our lock
        return real(script)

    monkeypatch.setattr(schema, "_statements", other_process_builds_first)
    schema.create_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] > 0


def test_019_creates_task_attachments_where_it_is_missing(tmp_path):
    """A database whose attachments table was never created gets it from 019."""
    db_path = tmp_path / "tracker.db"
    schema.create_database(db_path)
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        conn.execute("DROP TABLE task_attachments")
        conn.execute("DELETE FROM schema_migrations WHERE version = 19")
        assert [m.version for m in apply_all(conn, schema.MIGRATIONS_DIR)] == [19]
        columns = [row[1] for row in conn.execute("PRAGMA table_info(task_attachments)")]
        assert columns == ["id", "task_id", "filename", "stored_name", "mime_type",
                           "size_bytes", "uploaded_at", "uploaded_by"]
        assert "idx_attachments_task_id" in {row[1] for row in conn.execute("PRAGMA index_list(task_attachments)")}


def test_020_restores_the_project_delete_audit_that_006_dropped(tmp_path):
    """The live database lost audit_project_delete when 006 rebuilt projects."""
    db_path = tmp_path / "tracker.db"
    schema.create_database(db_path)
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        conn.execute("DROP TRIGGER audit_project_delete")
        conn.execute("DELETE FROM schema_migrations WHERE version = 20")
        assert [m.version for m in apply_all(conn, schema.MIGRATIONS_DIR)] == [20]
        conn.execute("INSERT INTO projects (id, name, path, status) VALUES ('p', 'P', '/p', 'active')")
        conn.execute("DELETE FROM projects WHERE id = 'p'")
        assert conn.execute(
            "SELECT table_name, deleted_id, source FROM delete_audit_log"
        ).fetchall() == [("projects", "p", "trigger")]


def test_calendar_entry_points_create_a_fresh_database(tmp_path, monkeypatch):
    """The calendar no longer creates its own tables; its managers must reach create_database."""
    from fastapi.testclient import TestClient

    from dashboard.app import app

    monkeypatch.setenv("PT_DB_PATH", str(tmp_path / "fresh" / "tracker.db"))
    (tmp_path / "fresh").mkdir()
    response = TestClient(app).get("/api/calendar/events")
    assert response.status_code == 200, response.text
    assert response.json()["events"] == []


@pytest.fixture
def live_guard(monkeypatch):
    """Run the fresh-database guard as outside the test suite."""
    monkeypatch.delenv("PT_ALLOW_FRESH_DB", raising=False)
    monkeypatch.delenv("PT_TEST_MODE", raising=False)
    monkeypatch.setattr(schema, "sys", SimpleNamespace(modules={k: v for k, v in sys.modules.items() if k != "pytest"}))


def test_a_new_install_is_not_refused_until_its_data_goes_missing(tmp_path, live_guard):
    """#8243: the marker comes with the first committed project, in the process that wrote it."""
    from scripts.db.manager import DatabaseManager

    db_path = tmp_path / "tracker.db"
    marker = tmp_path / ".db-fingerprint"
    schema.create_database(db_path)
    schema.create_database(db_path)  # an install's second command
    assert not marker.exists()
    DatabaseManager(db_path).add_project("p", "P", "/p", "active")
    first = marker.read_text()

    db_path.rename(tmp_path / "replaced.db")  # the database goes; the marker stays
    schema.create_database(db_path)
    with pytest.raises(schema.FreshDatabaseError, match="UNEXPECTED FRESH DATABASE"):
        schema.create_database(db_path)
    assert marker.read_text() == first


def test_opening_a_database_that_holds_data_leaves_the_marker(tmp_path, live_guard):
    """A database written without DatabaseManager (a restore, say) is marked when opened."""
    db_path = tmp_path / "tracker.db"
    schema.create_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("INSERT INTO projects (id, name, path, status) VALUES ('p', 'P', '/p', 'active')")
    assert not (tmp_path / ".db-fingerprint").exists()
    schema.create_database(db_path)
    assert (tmp_path / ".db-fingerprint").exists()
