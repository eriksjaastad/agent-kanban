"""Runtime safety regressions for explicit-ID schema and write paths."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# This module uses direct database access and direct schema inspection

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.backend_calendar_manager import CalendarManager as BackendCalendarManager  # noqa: E402
from db.attachment_paths import attachments_dir
from db.backend_manager import DatabaseManager as BackendDatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402


def _setup_db(tmp_path: Path) -> tuple[Path, BackendDatabaseManager, BackendCalendarManager]:
    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    return db_path, BackendDatabaseManager(db_path), BackendCalendarManager(db_path)


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_crr_insert_paths_assign_explicit_ids(monkeypatch, tmp_path: Path):
    ids = iter([101, 102, 103, 104, 105, 106, 107])
    monkeypatch.setattr("db.backend_manager.pt_next_id", lambda _db_path: next(ids))
    monkeypatch.setattr("db.backend_calendar_manager.pt_next_id", lambda _db_path: next(ids))

    db_path, db, cal = _setup_db(tmp_path)
    db.add_project("proj", "Proj", "/tmp/proj", "active")

    task = db.add_task("Task", "proj")
    assert task["id"] == 101

    attachment = db.add_attachment(task["id"], "file.txt", "stored.txt", "text/plain", 12)
    assert attachment["id"] == 103

    db.add_ai_agent("proj", "architect", "reviewer")
    db.add_service("proj", "github", "hosting", 0.0)
    db.set_info("stack", "python", project_id="proj")
    event_id = cal.add_event("Milestone", "2026-06-01", project_id="proj")

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT id FROM task_history").fetchone()[0] == 102
        assert conn.execute("SELECT id FROM ai_agents").fetchone()[0] == 104
        assert conn.execute("SELECT id FROM service_dependencies").fetchone()[0] == 105
        assert conn.execute("SELECT id FROM project_info").fetchone()[0] == 106
        assert event_id == 107
        assert conn.execute("SELECT id FROM calendar_events").fetchone()[0] == 107


def test_task_status_history_uses_explicit_ids(monkeypatch, tmp_path: Path):
    ids = iter([201, 202, 203])
    monkeypatch.setattr("db.backend_manager.pt_next_id", lambda _db_path: next(ids))

    db_path, db, _ = _setup_db(tmp_path)
    db.add_project("proj", "Proj", "/tmp/proj", "active")
    task = db.add_task("Task", "proj", status="Review")
    db.update_task(task["id"], status="Done")

    with sqlite3.connect(db_path) as conn:
        history_ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM task_history WHERE task_id = ? ORDER BY id",
                (task["id"],),
            ).fetchall()
        ]
    assert history_ids == [202, 203]


def test_add_project_rejects_duplicate_name(tmp_path: Path):
    _, db, _ = _setup_db(tmp_path)
    db.add_project("proj-a", "Shared Name", "/tmp/a", "active")

    with pytest.raises(ValueError, match="Project name 'Shared Name'"):
        db.add_project("proj-b", "Shared Name", "/tmp/b", "active")


def test_update_project_rejects_duplicate_name(tmp_path: Path):
    _, db, _ = _setup_db(tmp_path)
    db.add_project("proj-a", "Alpha", "/tmp/a", "active")
    db.add_project("proj-b", "Beta", "/tmp/b", "active")

    with pytest.raises(ValueError, match="Project name 'Alpha'"):
        db.update_project("proj-b", name="Alpha")


def test_get_info_collapses_duplicate_scope_key_rows(tmp_path: Path):
    db_path, db, _ = _setup_db(tmp_path)
    db.add_project("proj", "Proj", "/tmp/proj", "active")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO project_info (id, project_id, key, value, updated_at) VALUES (1, 'proj', 'stack', 'python', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO project_info (id, project_id, key, value, updated_at) VALUES (2, 'proj', 'stack', 'rust', '2026-01-02T00:00:00+00:00')"
        )
        conn.commit()

    entries = db.get_info(project_id="proj", key="stack")
    assert entries == [{
        "project_id": "proj",
        "key": "stack",
        "value": "rust",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }]


def test_delete_project_cleans_children_without_fk_cascade(tmp_path: Path):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path, db, cal = _setup_db(tmp_path)
    db.add_project("proj", "Proj", "/tmp/proj", "active")
    task = db.add_task("Task", "proj")
    db.add_attachment(task["id"], "file.txt", "stored.txt", "text/plain", 12)
    stored_file = attachments_dir(task["id"]) / "stored.txt"
    stored_file.write_text("payload")
    db.add_ai_agent("proj", "architect", "reviewer")
    db.add_service("proj", "github", "hosting", 0.0)
    cal.add_cron_job("proj", "0 * * * *", "echo hi", "demo")
    db.set_info("stack", "python", project_id="proj")
    event_id = cal.add_event("Milestone", "2026-06-01", project_id="proj")
    cal.link_task(event_id, task["id"])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO project_checkouts (project_id, machine, holder, checked_out_at) "
            "VALUES ('proj', 'mini', 'Qwen on Mini', '2026-10-16T00:00:00+00:00')"
        )

    db.delete_project("proj")

    with sqlite3.connect(db_path) as conn:
        assert _count(conn, "projects") == 0
        assert _count(conn, "tasks") == 0
        assert _count(conn, "task_history") == 0
        assert _count(conn, "task_attachments") == 0
        assert _count(conn, "calendar_event_tasks") == 0
        assert _count(conn, "project_info") == 0
        assert _count(conn, "project_checkouts") == 1
        checkout = conn.execute(
            "SELECT checked_in_at, checked_in_by FROM project_checkouts WHERE project_id = ?",
            ("proj",),
        ).fetchone()
        assert checkout[0] and checkout[1] == "project deletion"
        assert _count(conn, "ai_agents") == 0
        assert _count(conn, "service_dependencies") == 0
        assert _count(conn, "cron_jobs") == 0
        assert conn.execute("SELECT project_id FROM calendar_events WHERE id = ?", (event_id,)).fetchone()[0] is None
    assert not stored_file.exists()
    monkeypatch.undo()


def test_delete_task_cleans_descendants_and_children(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SAFE_MODE", "0")
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path, db, cal = _setup_db(tmp_path)
    db.add_project("proj", "Proj", "/tmp/proj", "active")
    parent = db.add_task("Parent", "proj")
    child = db.add_task("Child", "proj", parent_id=parent["id"])
    db.add_attachment(parent["id"], "p.txt", "p.bin", "text/plain", 1)
    db.add_attachment(child["id"], "c.txt", "c.bin", "text/plain", 1)
    parent_file = attachments_dir(parent["id"]) / "p.bin"
    child_file = attachments_dir(child["id"]) / "c.bin"
    parent_file.write_text("parent")
    child_file.write_text("child")
    event_id = cal.add_event("Milestone", "2026-06-01", project_id="proj")
    cal.link_task(event_id, parent["id"])
    cal.link_task(event_id, child["id"])

    db.delete_task(parent["id"])

    with sqlite3.connect(db_path) as conn:
        assert _count(conn, "tasks") == 0
        assert _count(conn, "task_history") == 0
        assert _count(conn, "task_attachments") == 0
        assert _count(conn, "calendar_event_tasks") == 0
    assert not parent_file.exists()
    assert not child_file.exists()


def test_delete_done_tasks_cleans_children(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SAFE_MODE", "0")
    monkeypatch.setenv("ALLOW_BULK_DELETE", "1")
    db_path, db, cal = _setup_db(tmp_path)
    db.add_project("proj", "Proj", "/tmp/proj", "active")
    done_task = db.add_task("Done task", "proj", status="Done")
    open_task = db.add_task("Open task", "proj", status="Backlog")
    db.add_attachment(done_task["id"], "done.txt", "done.bin", "text/plain", 1)
    event_id = cal.add_event("Milestone", "2026-06-01", project_id="proj")
    cal.link_task(event_id, done_task["id"])
    cal.link_task(event_id, open_task["id"])

    deleted = db.delete_done_tasks(project_id="proj")
    assert deleted == 1

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (done_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (open_task["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_history WHERE task_id = ?", (done_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_attachments WHERE task_id = ?", (done_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calendar_event_tasks WHERE task_id = ?", (done_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calendar_event_tasks WHERE task_id = ?", (open_task["id"],)).fetchone()[0] == 1


def test_trim_done_tasks_cleans_children(tmp_path: Path):
    db_path, db, cal = _setup_db(tmp_path)
    db.add_project("proj", "Proj", "/tmp/proj", "active")
    old_task = db.add_task("Old done", "proj", status="Done")
    keep_task = db.add_task("Keep done", "proj", status="Done")
    db.add_attachment(old_task["id"], "old.txt", "old.bin", "text/plain", 1)
    db.add_attachment(keep_task["id"], "keep.txt", "keep.bin", "text/plain", 1)
    event_id = cal.add_event("Milestone", "2026-06-01", project_id="proj")
    cal.link_task(event_id, old_task["id"])
    cal.link_task(event_id, keep_task["id"])

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET completed_at = ?, updated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00", "2026-01-01T00:00:00", old_task["id"]),
        )
        conn.execute(
            "UPDATE tasks SET completed_at = ?, updated_at = ? WHERE id = ?",
            ("2026-01-02T00:00:00", "2026-01-02T00:00:00", keep_task["id"]),
        )
        conn.commit()

    trimmed = db.trim_done_tasks(keep=1)
    assert trimmed == 1

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (old_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (keep_task["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_history WHERE task_id = ?", (old_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_attachments WHERE task_id = ?", (old_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calendar_event_tasks WHERE task_id = ?", (old_task["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calendar_event_tasks WHERE task_id = ?", (keep_task["id"],)).fetchone()[0] == 1
