"""Tests for subtasks and dependencies (Task #4645, #4579)."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from click.testing import CliRunner

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.manager import DatabaseManager
from db.schema import create_database
from pt import tasks_group


def _setup_db(tmp_path: Path) -> tuple[Path, DatabaseManager, str]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id="smart-invoice-workflow",
        name="Smart Invoice Workflow",
        path="/tmp/smart-invoice-workflow",
        status="active"
    )
    return db_path, db, "smart-invoice-workflow"


def test_manager_subtasks_and_progress(tmp_path: Path):
    db_path, db, project_id = _setup_db(tmp_path)

    parent = db.add_task(text="Parent task", project_id=project_id)
    db.add_task(text="Child one", project_id=project_id, parent_id=parent["id"], status="Done")
    db.add_task(text="Child two", project_id=project_id, parent_id=parent["id"], status="Backlog")

    subtasks = db.get_subtasks(parent["id"])
    assert len(subtasks) == 2
    assert all(t["parent_id"] == parent["id"] for t in subtasks)

    progress = db.get_subtask_progress(parent["id"])
    assert progress["total"] == 2
    assert progress["done"] == 1
    assert progress["percent"] == 50


def test_manager_blocking_relationships(tmp_path: Path):
    db_path, db, project_id = _setup_db(tmp_path)

    blocker_done = db.add_task(text="Blocker done", project_id=project_id, status="Done")
    blocker_open = db.add_task(text="Blocker open", project_id=project_id, status="Backlog")
    blocked = db.add_task(
        text="Blocked task",
        project_id=project_id,
        blocked_by=[blocker_done["id"], blocker_open["id"]]
    )

    blocking_tasks = db.get_blocking_tasks(blocked["id"])
    assert {t["id"] for t in blocking_tasks} == {blocker_done["id"], blocker_open["id"]}

    blocked_tasks = db.get_blocked_tasks(blocker_open["id"])
    assert {t["id"] for t in blocked_tasks} == {blocked["id"]}

    is_blocked, blocking_ids = db.is_blocked(blocked["id"])
    assert is_blocked is True
    assert blocking_ids == [blocker_open["id"]]


def test_cli_create_subtask_and_blocked_by(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db, project_id = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    parent = db.add_task(text="Parent task", project_id=project_id)
    blocker = db.add_task(text="Blocker task", project_id=project_id)

    runner = CliRunner()
    subtask_result = runner.invoke(
        tasks_group,
        [
            "create", "CLI subtask", "-p", project_id, "--parent", str(parent["id"]),
            "-d", "- [ ] Subtask work is reviewed and merged",
        ]
    )
    assert subtask_result.exit_code == 0

    subtasks = db.get_subtasks(parent["id"])
    assert any(t["text"] == "CLI subtask" for t in subtasks)

    blocked_result = runner.invoke(
        tasks_group,
        [
            "create", "CLI blocked", "-p", project_id, "--blocked-by", str(blocker["id"]),
            "-d", "- [ ] Blocker resolves and this card becomes unblocked",
        ]
    )
    assert blocked_result.exit_code == 0

    blocked_task = next(t for t in db.get_tasks(project_id=project_id) if t["text"] == "CLI blocked")
    assert json.loads(blocked_task["blocked_by"]) == [blocker["id"]]


def test_api_subtasks_and_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db, project_id = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    from dashboard.app import app

    parent = db.add_task(text="API parent", project_id=project_id)
    db.add_task(text="API child", project_id=project_id, parent_id=parent["id"], status="Done")

    blocker = db.add_task(text="API blocker", project_id=project_id)
    blocked = db.add_task(
        text="API blocked",
        project_id=project_id,
        blocked_by=[blocker["id"]],
        prompt="Do work"
    )

    client = TestClient(app)

    parent_response = client.get(f"/api/tasks/{parent['id']}")
    assert parent_response.status_code == 200
    parent_payload = parent_response.json()
    assert parent_payload["display_id"] == db.get_task_display_id(parent["id"])
    assert parent_payload["subtasks"]
    assert parent_payload["subtasks"][0]["display_id"] == db.get_task_display_id(parent_payload["subtasks"][0]["id"])
    assert parent_payload["subtask_progress"]["done"] == 1

    blocked_response = client.get(f"/api/tasks/{blocked['id']}")
    assert blocked_response.status_code == 200
    blocked_payload = blocked_response.json()
    assert blocked_payload["display_id"] == db.get_task_display_id(blocked["id"])
    assert blocked_payload["blocked_by_display_ids"] == [db.get_task_display_id(blocker["id"])]
    assert blocked_payload["blocking_tasks"]
    assert blocked_payload["blocking_tasks"][0]["display_id"] == db.get_task_display_id(blocker["id"])
    assert blocked_payload["is_blocked"] is True

    update_response = client.patch(
        f"/api/tasks/{blocked['id']}",
        json={"blocked_by": [blocker["id"]], "parent_id": parent["id"]}
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    # str(): the API stringifies Snowflake-scale ids on the wire (#7824) --
    # parent["id"] here is the raw int from the DatabaseManager layer.
    assert updated["parent_id"] == str(parent["id"])
    assert updated["parent_display_id"] == db.get_task_display_id(parent["id"])
    assert json.loads(updated["blocked_by"]) == [blocker["id"]]

    start_response = client.patch(
        f"/api/tasks/{blocked['id']}",
        json={"status": "In Progress"}
    )
    assert start_response.status_code == 400
    detail = start_response.json().get("detail", "").lower()
    assert "blocked by" in detail
    assert f"#{db.get_task_display_id(blocker['id'])}" in detail


# ---------------------------------------------------------------------
# #6873 — archived cards must not leak into subtask/blocked queries
# ---------------------------------------------------------------------


def _archive(db: DatabaseManager, task_id: int) -> None:
    """Mark one card archived, the way trim_done_tasks does."""
    from datetime import datetime, timezone
    db.seed(task_id, archived_at=datetime.now(timezone.utc).isoformat())


def test_get_subtasks_excludes_archived_by_default(tmp_path: Path):
    """An archived Done subtask used to inflate the progress denominator,
    so a fully-complete parent rendered as e.g. 4/7."""
    db_path, db, project_id = _setup_db(tmp_path)

    parent = db.add_task(text="Parent", project_id=project_id)
    live = db.add_task(
        text="Live child", project_id=project_id,
        parent_id=parent["id"], status="Done",
    )
    stale = db.add_task(
        text="Archived child", project_id=project_id,
        parent_id=parent["id"], status="Done",
    )
    _archive(db, stale["id"])

    subtasks = db.get_subtasks(parent["id"])
    assert [t["id"] for t in subtasks] == [live["id"]]

    with_archived = db.get_subtasks(parent["id"], include_archived=True)
    assert {t["id"] for t in with_archived} == {live["id"], stale["id"]}


def test_get_subtask_progress_passes_include_archived_through(tmp_path: Path):
    db_path, db, project_id = _setup_db(tmp_path)

    parent = db.add_task(text="Parent", project_id=project_id)
    db.add_task(
        text="Done child", project_id=project_id,
        parent_id=parent["id"], status="Done",
    )
    stale = db.add_task(
        text="Archived child", project_id=project_id,
        parent_id=parent["id"], status="Done",
    )
    _archive(db, stale["id"])

    progress = db.get_subtask_progress(parent["id"])
    assert (progress["done"], progress["total"], progress["percent"]) == (1, 1, 100)

    everything = db.get_subtask_progress(parent["id"], include_archived=True)
    assert (everything["done"], everything["total"]) == (2, 2)


def test_get_blocked_tasks_excludes_archived_by_default(tmp_path: Path):
    db_path, db, project_id = _setup_db(tmp_path)

    blocker = db.add_task(text="Blocker", project_id=project_id, status="Backlog")
    live = db.add_task(
        text="Live blocked", project_id=project_id, blocked_by=[blocker["id"]],
    )
    stale = db.add_task(
        text="Archived blocked", project_id=project_id,
        blocked_by=[blocker["id"]], status="Done",
    )
    _archive(db, stale["id"])

    assert [t["id"] for t in db.get_blocked_tasks(blocker["id"])] == [live["id"]]
    assert {
        t["id"] for t in db.get_blocked_tasks(blocker["id"], include_archived=True)
    } == {live["id"], stale["id"]}
