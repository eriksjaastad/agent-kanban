"""Tests for the dashboard's Kanban breakdown (#7181).

The dashboard's top section reports how many open cards each project is
carrying in each board column. Two things have to hold for that section to be
worth trusting: the counts must be the board's counts (no Done, no Cancelled,
no archived rows), and each column must arrive ranked so the biggest pile is
first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app
from db.manager import DatabaseManager
from db.schema import create_database


def _setup_db(tmp_path: Path) -> tuple[Path, DatabaseManager]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id="project-tracker",
        name="Project Tracker",
        path=str(tmp_path / "project-tracker"),
        status="active",
    )
    db.add_project(
        project_id="holoapp",
        name="holoapp",
        path=str(tmp_path / "holoapp"),
        status="active",
    )
    return db_path, db


def _breakdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path) -> dict:
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    response = TestClient(dashboard_app.app).get("/api/kanban/breakdown")
    assert response.status_code == 200, response.text
    return response.json()


def test_breakdown_counts_each_project_per_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)

    for _ in range(3):
        db.add_task("backlog card", "holoapp", status="Backlog")
    db.add_task("backlog card", "project-tracker", status="Backlog")
    db.add_task("todo card", "project-tracker", status="To Do")
    db.add_task("wip card", "holoapp", status="In Progress")

    payload = _breakdown(tmp_path, monkeypatch, db_path)

    assert payload["statuses"] == ["Backlog", "To Do", "In Progress", "Review"]
    assert payload["totals"] == {"Backlog": 4, "To Do": 1, "In Progress": 1, "Review": 0}
    assert payload["columns"]["Backlog"] == [
        {"project_id": "holoapp", "project": "holoapp", "count": 3},
        {"project_id": "project-tracker", "project": "Project Tracker", "count": 1},
    ]
    assert payload["columns"]["Review"] == []


def test_breakdown_ranks_projects_by_card_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)

    # Insert the small pile first: ranking must come from the counts, not from
    # insertion order or SQLite's grouping order.
    db.add_task("only card", "project-tracker", status="Backlog")
    for _ in range(5):
        db.add_task("backlog card", "holoapp", status="Backlog")

    payload = _breakdown(tmp_path, monkeypatch, db_path)

    counts = [entry["count"] for entry in payload["columns"]["Backlog"]]
    assert counts == [5, 1]


def test_breakdown_excludes_closed_and_archived_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)

    db.add_task("open card", "holoapp", status="Backlog")
    db.add_task("finished card", "holoapp", status="Done")
    db.add_task("dropped card", "holoapp", status="Cancelled")

    payload = _breakdown(tmp_path, monkeypatch, db_path)

    assert payload["totals"] == {"Backlog": 1, "To Do": 0, "In Progress": 0, "Review": 0}
    assert set(payload["columns"]) == {"Backlog", "To Do", "In Progress", "Review"}


def test_counts_drop_archived_rows_unless_asked(tmp_path: Path):
    """Archived Done cards still exist; they just are not on the board."""
    _, db = _setup_db(tmp_path)

    db.add_task("finished card", "holoapp", status="Done")
    assert db.archive_done_tasks(keep_per_project=0) == 1

    visible = db.get_task_counts_by_project(statuses=["Done"])
    archived = db.get_task_counts_by_project(statuses=["Done"], include_archived=True)

    assert visible == []
    assert archived == [{"project_id": "holoapp", "status": "Done", "count": 1}]
