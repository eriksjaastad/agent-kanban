"""An unreadable tasks.blocked_by fails closed without crashing (#6900).

`get_blocking_tasks` used to swallow a malformed value and return [], so
`is_blocked` answered ``(False, [])``: `pt tasks start` and the dashboard let a
card start that was supposed to be blocked, and the task detail endpoint
silently dropped its blocking fields. The fix makes such a card BLOCKED, with a
visible reason, while the listings that render it keep working.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import asyncio

import pytest
from click.testing import CliRunner
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402
from pt import tasks_group  # noqa: E402

PROJECT = "alpha"
MALFORMED = "7,8"  # what a hand edit or an old CLI could leave behind


@pytest.fixture
def board(tmp_path, monkeypatch):
    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(PROJECT, "Alpha", str(tmp_path / PROJECT), "active")
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    db.db_file = db_path
    return db


def _force_blocked_by(db, task_id, value):
    """Write blocked_by past every validator, as a pre-fix row would be."""
    conn = sqlite3.connect(db.db_file)
    try:
        conn.execute("UPDATE tasks SET blocked_by = ? WHERE id = ?", (value, task_id))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def malformed_card(board):
    task = board.add_task("Malformed dependency", PROJECT, status="To Do")
    _force_blocked_by(board, task["id"], MALFORMED)
    return task


@pytest.mark.parametrize("value", [MALFORMED, "5", '{"a": 1}'])
def test_unreadable_blocked_by_counts_as_blocked_with_reason(board, value):
    task = board.add_task("Card", PROJECT)
    _force_blocked_by(board, task["id"], value)
    assert board.is_blocked(task["id"]) == (True, [])
    assert board.blocked_by_error(task["id"]) == f"blocked_by unreadable: {value!r}"


def test_readable_blocked_by_has_no_error(board):
    blocker = board.add_task("Blocker", PROJECT)
    task = board.add_task("Card", PROJECT, blocked_by=[blocker["id"]])
    assert board.blocked_by_error(task["id"]) is None
    assert board.is_blocked(task["id"]) == (True, [blocker["id"]])


def test_pt_tasks_start_refuses_with_the_reason(board, malformed_card):
    display_id = board.get_task_display_id(malformed_card["id"])
    result = CliRunner().invoke(tasks_group, ["start", str(display_id)])
    assert result.exit_code == 1, result.output  # refused start fails (#8093)
    assert "Cannot start" in result.output
    assert "blocked_by unreadable" in result.output
    assert MALFORMED in result.output
    assert board.get_task(malformed_card["id"])["status"] == "To Do"


def test_pt_tasks_listing_renders_the_card_as_blocked(board, malformed_card):
    board.add_task("Healthy card", PROJECT, status="To Do")
    result = CliRunner().invoke(tasks_group, ["-p", PROJECT])
    assert result.exit_code == 0, result.output
    line = next(l for l in result.output.splitlines() if "Malformed dependency" in l)
    assert "[B:unreadable]" in line
    assert any("Healthy card" in l for l in result.output.splitlines())


def test_pt_tasks_listing_survives_a_failing_blocked_lookup(board, malformed_card, monkeypatch):
    """The old `except Exception: unblocked` at the first pass is now fail-closed."""
    from db.operations import ProjectTrackerOps

    def broken(self, task_id):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(ProjectTrackerOps, "is_blocked", broken, raising=False)
    result = CliRunner().invoke(tasks_group, ["-p", PROJECT])
    assert result.exit_code == 0, result.output
    line = next(l for l in result.output.splitlines() if "Malformed dependency" in l)
    assert "[B:unreadable]" in line


def test_pt_tasks_tree_renders_the_reason(board):
    parent = board.add_task("Parent", PROJECT)
    child = board.add_task("Child", PROJECT, parent_id=parent["id"])
    _force_blocked_by(board, child["id"], MALFORMED)
    result = CliRunner().invoke(
        tasks_group, ["tree", str(board.get_task_display_id(parent["id"]))]
    )
    assert result.exit_code == 0, result.output
    assert "blocked: blocked_by unreadable" in result.output
    assert "READY NOW" not in result.output



def _run(coro):
    # asyncio.run() would clear the loop other dashboard tests reuse.
    return asyncio.get_event_loop_policy().get_event_loop().run_until_complete(coro)

def test_dashboard_start_refuses_with_400_and_the_reason(board, malformed_card, monkeypatch):
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "DatabaseManager", lambda *a, **k: board)
    request = dashboard_app.TaskUpdateRequest(status="In Progress")
    with pytest.raises(HTTPException) as excinfo:
        # Write handlers are async wrappers that run on the single write thread.
        _run(dashboard_app.update_task(malformed_card["id"], request))
    assert excinfo.value.status_code == 400
    assert "blocked_by unreadable" in excinfo.value.detail
    assert board.get_task(malformed_card["id"])["status"] == "To Do"


def test_dashboard_task_detail_shows_the_blocking_error(board, malformed_card, monkeypatch):
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "DatabaseManager", lambda *a, **k: board)
    payload = dashboard_app.get_task(malformed_card["id"])
    assert payload["is_blocked"] is True
    assert payload["blocked_by_ids"] == []
    assert payload["incomplete_blocking_ids"] == []
    assert payload["blocking_tasks"] == []
    assert payload["blocked_by_error"] == f"blocked_by unreadable: {MALFORMED!r}"


def test_dashboard_board_listing_renders_the_card_blocked(board, malformed_card, monkeypatch):
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "DatabaseManager", lambda *a, **k: board)
    payload = dashboard_app.list_tasks(project_id=PROJECT, status_filter=None)
    rows = payload["tasks"] if isinstance(payload, dict) else payload
    row = next(r for r in rows if r["id"] == str(malformed_card["id"]))
    assert row["is_blocked"] is True
    assert "blocked_by unreadable" in row["blocked_by_error"]
