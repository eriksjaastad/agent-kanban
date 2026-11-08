"""Tests for the proposal task type feature.

Covers:
- validate_task_type validation for valid/invalid types
- Creating proposal tasks via DatabaseManager
- Default task listing excludes proposals
- --proposals flag returns only proposals
- Approve converts proposal to manual type
- Reject cancels a proposal
"""

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.manager import DatabaseManager
from db.schema import create_database
from pt import tasks_group
from utils.validation import validate_task_type, VALID_TASK_TYPES


def _setup_db(tmp_path: Path) -> tuple[Path, DatabaseManager, str]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id="test-project",
        name="Test Project",
        path="/tmp/test-project",
        status="active",
    )
    return db_path, db, "test-project"


# --- validate_task_type tests ---


class TestValidateTaskType:
    @pytest.mark.parametrize("task_type", ["proposal", "manual", "agent", None], ids=["proposal", "manual", "agent", "none"])
    def test_valid_type_accepted(self, task_type):
        is_valid, error = validate_task_type(task_type)
        assert is_valid is True
        assert error is None

    @pytest.mark.parametrize("task_type", ["invalid", ""], ids=["invalid_type", "empty_string"])
    def test_invalid_type_rejected(self, task_type):
        is_valid, error = validate_task_type(task_type)
        assert is_valid is False
        assert "Task type must be one of" in error

    def test_valid_types_constant(self):
        assert "proposal" in VALID_TASK_TYPES
        assert "manual" in VALID_TASK_TYPES
        assert "agent" in VALID_TASK_TYPES


# --- DatabaseManager proposal creation ---


class TestProposalCreation:
    def test_create_proposal_task(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        task = db.add_task(
            text="Add caching layer",
            project_id=project_id,
            task_type="proposal",
        )
        assert task["task_type"] == "proposal"
        assert task["status"] == "Backlog"

    def test_create_manual_task_default(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        task = db.add_task(text="Fix bug", project_id=project_id)
        assert task["task_type"] == "manual"

    def test_get_proposal_task(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        task = db.add_task(
            text="Proposal task",
            project_id=project_id,
            task_type="proposal",
        )
        fetched = db.get_task(task["id"])
        assert fetched is not None
        assert fetched["task_type"] == "proposal"


# --- Default listing excludes proposals ---


class TestProposalFiltering:
    def test_default_listing_excludes_proposals(self, tmp_path: Path):
        db_path, db, project_id = _setup_db(tmp_path)
        db.add_task(text="Regular task", project_id=project_id)
        db.add_task(text="Proposal task", project_id=project_id, task_type="proposal")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        # Prevent auto-detection from cwd
        monkeypatch.setattr("commands.tasks._detect_project_from_cwd", lambda db: None)

        runner = CliRunner()
        result = runner.invoke(tasks_group, ["-p", "test-project"])
        assert result.exit_code == 0
        assert "Regular task" in result.output
        assert "Proposal task" not in result.output
        monkeypatch.undo()

    def test_proposals_flag_shows_only_proposals(self, tmp_path: Path):
        db_path, db, project_id = _setup_db(tmp_path)
        db.add_task(text="Regular task", project_id=project_id)
        db.add_task(text="Proposal task", project_id=project_id, task_type="proposal")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        monkeypatch.setattr("commands.tasks._detect_project_from_cwd", lambda db: None)

        runner = CliRunner()
        result = runner.invoke(tasks_group, ["-p", "test-project", "--proposals"])
        assert result.exit_code == 0
        assert "Proposal task" in result.output
        assert "Regular task" not in result.output
        monkeypatch.undo()


# --- Approve command ---


class TestApproveCommand:
    def test_approve_converts_to_manual(self, tmp_path: Path):
        db_path, db, project_id = _setup_db(tmp_path)
        task = db.add_task(
            text="Proposed feature",
            project_id=project_id,
            task_type="proposal",
        )

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("PT_DB_PATH", str(db_path))

        runner = CliRunner()
        result = runner.invoke(tasks_group, ["approve", str(task["id"])])
        assert result.exit_code == 0
        assert "Approved" in result.output

        updated = db.get_task(task["id"])
        assert updated["task_type"] == "manual"
        # Status should remain unchanged (Backlog)
        assert updated["status"] == "Backlog"
        monkeypatch.undo()


# --- Reject command ---


class TestRejectCommand:
    def test_reject_cancels_proposal(self, tmp_path: Path):
        db_path, db, project_id = _setup_db(tmp_path)
        task = db.add_task(
            text="Bad proposal",
            project_id=project_id,
            task_type="proposal",
        )

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("PT_DB_PATH", str(db_path))

        runner = CliRunner()
        result = runner.invoke(tasks_group, ["reject", str(task["id"])])
        assert result.exit_code == 0
        assert "Rejected" in result.output

        updated = db.get_task(task["id"])
        assert updated["status"] == "Cancelled"
        monkeypatch.undo()


# --- approve/reject refuse non-proposals and missing tasks ---


class TestApproveRejectGuards:
    @pytest.mark.parametrize("command", ["approve", "reject"])
    def test_rejects_non_proposal(self, tmp_path: Path, monkeypatch, command):
        db_path, db, project_id = _setup_db(tmp_path)
        task = db.add_task(text="Regular task", project_id=project_id)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))

        result = CliRunner().invoke(tasks_group, [command, str(task["id"])])
        assert "not a proposal" in result.output

    @pytest.mark.parametrize("command", ["approve", "reject"])
    def test_nonexistent_task(self, tmp_path: Path, monkeypatch, command):
        db_path, db, _ = _setup_db(tmp_path)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))

        result = CliRunner().invoke(tasks_group, [command, "99999"])
        assert "not found" in result.output
