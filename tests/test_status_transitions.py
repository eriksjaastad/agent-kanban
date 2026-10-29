"""Tests for task status transitions state machine."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.manager import DatabaseManager
from db.schema import create_database
from pt import tasks_group


def _setup_db(tmp_path: Path) -> tuple[Path, DatabaseManager, str]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id="project-tracker",
        name="Project Tracker",
        path="/tmp/project-tracker",
        status="active"
    )
    return db_path, db, "project-tracker"


def test_manager_transition_validation(tmp_path: Path):
    db_path, db, project_id = _setup_db(tmp_path)

    task = db.add_task(text="Transition task", project_id=project_id, status="Backlog")

    # Valid: Backlog -> To Do
    updated = db.update_task(task["id"], status="To Do")
    assert updated["status"] == "To Do"

    # Invalid: To Do -> Done (skips Review)
    with pytest.raises(ValueError) as exc:
        db.update_task(task["id"], status="Done")
    assert "Cannot move from To Do to Done" in str(exc.value)


def test_api_transition_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db, project_id = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    from dashboard.app import app

    task = db.add_task(text="API transition", project_id=project_id, status="Backlog")
    client = TestClient(app)

    response = client.patch(f"/api/tasks/{task['id']}", json={"status": "Done"})
    assert response.status_code == 400
    assert "Cannot move from Backlog to Done" in response.json().get("message", "")


def test_cli_transition_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db, project_id = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    task = db.add_task(
        text="CLI transition",
        project_id=project_id,
        status="Backlog",
        prompt="Ready to work"
    )

    runner = CliRunner()
    result = runner.invoke(tasks_group, ["start", str(task["id"])])
    assert result.exit_code == 1  # a refused transition is a failure (#8093)
    assert "Cannot move from Backlog to In Progress" in result.output
