"""What the app relies on in the schema every database starts from (schema.sql).

These facts were checked by the tests of the migrations that first made them
true (003-018); they stay pinned here, on the schema itself, so retiring a
migration never retires the check on what it left behind.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

CORE_TABLES = (
    "tasks", "ideas", "task_history", "task_attachments", "projects", "project_info",
    "ai_agents", "service_dependencies", "calendar_events", "calendar_event_tasks",
)


@pytest.fixture
def conn(isolated_database):
    with closing(sqlite3.connect(isolated_database)) as conn:
        yield conn


def _columns(conn, table):
    return conn.execute(f"PRAGMA table_info({table})").fetchall()


def test_not_null_columns_have_defaults_and_status_starts_in_backlog(conn):
    """Inserts that omit a column still succeed (003/004/007/008)."""
    for table in CORE_TABLES:
        for _cid, name, _type, notnull, default, pk in _columns(conn, table):
            if pk:
                assert notnull, f"{table}.{name}: primary key allows NULL"
            elif notnull:
                assert default is not None, f"{table}.{name}: NOT NULL without a DEFAULT"
    conn.execute("INSERT INTO tasks (id, text, project_id) VALUES (1, 'no status', 'p')")
    assert conn.execute("SELECT status FROM tasks WHERE id = 1").fetchone() == ("Backlog",)
    with pytest.raises(sqlite3.IntegrityError):  # the CHECK constraints survived 003-006's rebuilds
        conn.execute("INSERT INTO tasks (id, text, project_id, status) VALUES (2, 'x', 'p', 'Someday')")


def test_links_are_plain_columns_without_foreign_keys_or_extra_unique_indexes(conn):
    """Child rows are guarded by INSERT ... WHERE EXISTS, not FKs (005/006)."""
    for table in CORE_TABLES:
        assert conn.execute(f"PRAGMA foreign_key_list({table})").fetchall() == [], table
    assert {"project_id", "parent_id"} <= {c[1] for c in _columns(conn, "tasks")}
    conn.execute("INSERT INTO tasks (id, text, project_id) VALUES (2, 'parent', 'p')")
    conn.execute("INSERT INTO tasks (id, text, project_id, parent_id) VALUES (3, 'child', 'p', 2)")
    assert conn.execute("SELECT parent_id FROM tasks WHERE id = 3").fetchone() == (2,)
    assert {"task_id", "project_id"} <= {c[1] for c in _columns(conn, "task_history")}
    for table in ("projects", "project_info"):
        unique = [row[1] for row in conn.execute(f"PRAGMA index_list({table})") if row[2] and row[3] != "pk"]
        assert unique == [], f"{table}: {unique}"


def test_lookup_indexes_exist(conn):
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {
        "idx_tasks_project", "idx_tasks_status", "idx_tasks_parent", "idx_ai_agents_project",
        "idx_service_deps_project", "idx_project_info_project", "idx_project_info_scope_key",
        "idx_projects_status", "idx_projects_name", "idx_projects_last_modified",
        "idx_history_event", "idx_history_project", "idx_history_timestamp",
        "idx_attachments_task_id", "idx_cal_date",
    } <= names


def test_task_deletes_are_blocked_unless_permitted_and_then_audited(conn):
    conn.execute("INSERT INTO tasks (id, text, project_id) VALUES (1, 'keep', 'p')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM tasks WHERE id = 1")
    conn.execute("UPDATE _delete_permissions SET enabled = 1 WHERE id = 1")
    conn.execute("DELETE FROM tasks WHERE id = 1")
    assert conn.execute(
        "SELECT COUNT(*) FROM delete_audit_log WHERE table_name = 'tasks'"
    ).fetchone() == (1,)


def test_tasks_carry_archived_at(conn):
    assert "archived_at" in {c[1] for c in _columns(conn, "tasks")}


def test_codebase_snapshots_are_one_row_per_project_per_run_and_append_only(conn):
    assert [c[1] for c in _columns(conn, "codebase_size_snapshots")] == [
        "id", "run_id", "snapshot_date", "project", "code_lines", "test_lines",
        "doc_files", "doc_lines", "last_commit", "commits_90d", "scanned_at",
    ]
    insert = (
        "INSERT INTO codebase_size_snapshots (run_id, snapshot_date, project, code_lines,"
        " test_lines, doc_files, doc_lines, scanned_at) VALUES ('r', 'd', 'p', ?, 0, 0, 0, 't')"
    )
    conn.execute(insert, (0,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (1,))
    for statement in ("UPDATE codebase_size_snapshots SET code_lines = 2", "DELETE FROM codebase_size_snapshots"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)


def test_job_kind_is_job_or_volunteer(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (company, title, url, kind) VALUES ('X', 'Y', 'https://example.test/x', 'gig')")


def test_core_tables_have_no_autoincrement(conn):
    """Explicit IDs (pt_id) are written into every key; AUTOINCREMENT was removed in 003."""
    for table in CORE_TABLES:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
        assert "AUTOINCREMENT" not in sql.upper(), table


def test_timestamp_and_title_columns_default_to_empty_text(conn):
    """The exact defaults 007 and 008 added, so an insert that omits them still works."""
    for table, names in (("tasks", ("created_at", "updated_at")),
                         ("calendar_events", ("created_at", "updated_at", "title", "event_date"))):
        defaults = {c[1]: c[4] for c in _columns(conn, table)}
        assert {name: defaults[name] for name in names} == dict.fromkeys(names, "''"), table


def test_jobs_tables_have_their_columns_and_indexes(conn):
    """The jobs and job_submissions shape 013 and 016 built, columns and indexes exactly."""
    assert _columns(conn, "jobs") == [
        (0, "id", "INTEGER", 0, None, 1), (1, "company", "TEXT", 1, None, 0), (2, "title", "TEXT", 1, None, 0),
        (3, "location", "TEXT", 0, None, 0), (4, "url", "TEXT", 1, None, 0),
        (5, "source", "TEXT", 1, "'manual'", 0), (6, "posted_date", "TEXT", 0, None, 0),
        (7, "first_seen", "TEXT", 1, "date('now')", 0), (8, "category", "TEXT", 1, "'Other'", 0),
        (9, "raw", "TEXT", 0, None, 0), (10, "deleted_at", "TEXT", 0, None, 0),
        (11, "kind", "TEXT", 1, "'job'", 0), (12, "volunteering_since", "TEXT", 0, None, 0),
    ]
    assert _columns(conn, "job_submissions") == [
        (0, "id", "INTEGER", 0, None, 1), (1, "job_id", "INTEGER", 1, None, 0),
        (2, "submitted_at", "TEXT", 1, None, 0), (3, "resume_path", "TEXT", 0, None, 0),
        (4, "cover_letter_path", "TEXT", 0, None, 0), (5, "notes", "TEXT", 0, None, 0),
    ]
    indexes = {
        name: (unique, [col[2] for col in conn.execute(f"PRAGMA index_info({name})")])
        for table in ("jobs", "job_submissions")
        for _seq, name, unique, _origin, _partial in conn.execute(f"PRAGMA index_list({table})")
    }
    assert indexes == {
        "idx_jobs_category": (0, ["category"]), "idx_jobs_deleted_at": (0, ["deleted_at"]),
        "idx_jobs_first_seen": (0, ["first_seen"]), "idx_jobs_kind": (0, ["kind"]),
        "sqlite_autoindex_jobs_1": (1, ["url"]),
        "idx_job_submissions_job_submitted": (0, ["job_id", "submitted_at"]),
        "idx_job_submissions_submitted_at": (0, ["submitted_at"]),
    }


def test_every_table_matches_the_pinned_schema(conn):
    """Every column's type, NOT NULL, default and key position, and every index's
    unique, origin and partial flags and columns, as tests/schema_contract.json
    pins them. A migration that changes the schema updates that file in the
    same commit; anything else that changes it fails here.
    """
    actual = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        indexes = {
            name: [unique, origin, partial, [col[2] for col in conn.execute(f'PRAGMA index_info("{name}")')]]
            for _seq, name, unique, origin, partial in conn.execute(f'PRAGMA index_list("{table}")')
        }
        actual[table] = {"columns": [list(col) for col in _columns(conn, f'"{table}"')], "indexes": indexes}
    pinned = json.loads((Path(__file__).parent / "schema_contract.json").read_text())
    assert sorted(actual) == sorted(pinned)
    for table in pinned:
        assert actual[table] == pinned[table], table
