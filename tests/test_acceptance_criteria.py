"""Tests for mandatory acceptance criteria on new cards (#7608).

Covers:
- validate_acceptance_criteria() unit behavior (valid/invalid checklist lines)
- DatabaseManager.create_card() enforces it; add_task() stays permissive
  (so ~100 internal/test fixtures calling add_task() directly keep working)
- update_task() is never validated, so an old card with no criteria stays
  editable with no retroactive failure
- pt tasks create (CLI) rejects/accepts, including --proposal (not exempt)
- POST /api/tasks (dashboard API) rejects/accepts, with a useful field name
  on the 400 response
"""

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


from db.manager import DatabaseManager
from db.schema import create_database
from pt import tasks_group
from utils.validation import validate_acceptance_criteria, ACCEPTANCE_CRITERIA_ERROR

VALID_NOTES = "- [ ] Run pytest tests/test_foo.py and confirm it passes"


def _setup_db(tmp_path: Path, project_id: str = "test-project") -> tuple[Path, DatabaseManager, str]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id=project_id,
        name="Test Project",
        path=f"/tmp/{project_id}",
        status="active",
    )
    return db_path, db, project_id


# --- validate_acceptance_criteria() unit tests ---


class TestValidateAcceptanceCriteria:
    @pytest.mark.parametrize(
        "notes",
        [
            "- [ ] pytest passes",
            "- [x] already verified manually",
            "- [X] already verified manually",
            "Some context first.\n\n- [ ] pytest passes\n\nMore context after.",
        ],
        ids=["unchecked_item", "checked_item", "uppercase_checked_item", "item_among_other_notes"],
    )
    def test_valid_notes_accepted(self, notes):
        is_valid, error = validate_acceptance_criteria(notes)
        assert is_valid is True
        assert error is None

    @pytest.mark.parametrize(
        "notes",
        [None, "", "   \n  \n", "- [ ] ", "- Just a bullet, not a checkbox"],
        ids=["none", "empty_string", "whitespace_only", "checkbox_with_no_text", "plain_bullet_without_checkbox"],
    )
    def test_invalid_notes_rejected(self, notes):
        is_valid, error = validate_acceptance_criteria(notes)
        assert is_valid is False
        assert error == ACCEPTANCE_CRITERIA_ERROR

    def test_heading_alone_rejected(self):
        """A magic heading with no items under it must not satisfy the check."""
        is_valid, error = validate_acceptance_criteria("## Acceptance Criteria")
        assert is_valid is False
        assert "heading alone" in error

    def test_error_names_the_exact_format(self):
        """Error message must say exactly what to add, with an example line."""
        is_valid, error = validate_acceptance_criteria(None)
        assert "- [ ] <how completion is verified>" in error

    def test_error_tells_proposals_what_to_do(self):
        is_valid, error = validate_acceptance_criteria(None)
        assert "decision/approval criterion" in error


# --- DatabaseManager.create_card() vs add_task() ---


class TestCreateCardEnforcement:
    def test_create_card_rejects_missing_notes(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        with pytest.raises(ValueError, match="Acceptance criteria required"):
            db.create_card(text="New feature", project_id=project_id)

    def test_create_card_rejects_heading_only_notes(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        with pytest.raises(ValueError, match="Acceptance criteria required"):
            db.create_card(text="New feature", project_id=project_id, notes="## Acceptance Criteria")

    def test_create_card_accepts_valid_notes(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        task = db.create_card(text="New feature", project_id=project_id, notes=VALID_NOTES)
        assert task["notes"] == VALID_NOTES

    def test_create_card_does_not_exempt_proposals(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        with pytest.raises(ValueError, match="Acceptance criteria required"):
            db.create_card(text="Proposed idea", project_id=project_id, task_type="proposal")
        # ...but succeeds once a decision criterion is given
        task = db.create_card(
            text="Proposed idea",
            project_id=project_id,
            task_type="proposal",
            notes="- [ ] Approve if load drops below 200ms; reject otherwise",
        )
        assert task["task_type"] == "proposal"

    def test_create_card_does_not_exempt_subtasks(self, tmp_path: Path):
        _, db, project_id = _setup_db(tmp_path)
        parent = db.create_card(text="Parent", project_id=project_id, notes=VALID_NOTES)
        with pytest.raises(ValueError, match="Acceptance criteria required"):
            db.create_card(text="Child", project_id=project_id, parent_id=parent["id"])
        child = db.create_card(
            text="Child", project_id=project_id, parent_id=parent["id"], notes=VALID_NOTES
        )
        assert child["parent_id"] == parent["id"]

    def test_add_task_stays_permissive(self, tmp_path: Path):
        """The low-level DB primitive must keep working with no notes at all —
        this is what ~100 existing internal/test call sites rely on."""
        _, db, project_id = _setup_db(tmp_path)
        task = db.add_task(text="Internal fixture task", project_id=project_id)
        assert task["notes"] is None

    def test_update_task_never_requires_criteria(self, tmp_path: Path):
        """An old card created before #7608 (no criteria) must stay editable
        with no retroactive failure and no bulk migration."""
        _, db, project_id = _setup_db(tmp_path)
        old_card = db.add_task(text="Pre-#7608 card", project_id=project_id)
        assert old_card["notes"] is None

        updated = db.update_task(old_card["id"], text="Pre-#7608 card, lightly edited")
        assert updated["text"] == "Pre-#7608 card, lightly edited"
        assert updated["notes"] is None

        # Editing something else entirely (status) on the same criteria-less
        # card must not be blocked either.
        moved = db.update_task(old_card["id"], status="To Do")
        assert moved["status"] == "To Do"


# --- CLI: pt tasks create ---


class TestCliTasksCreate:
    def test_rejects_without_description(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_path, db, project_id = _setup_db(tmp_path)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        monkeypatch.setattr("pt._detect_project_from_cwd", lambda db: None)

        runner = CliRunner()
        result = runner.invoke(tasks_group, ["create", "New feature", "-p", project_id])
        assert result.exit_code != 0, "a rejected create must not exit 0"
        assert "Acceptance criteria required" in result.output
        assert "- [ ] <how completion is verified>" in result.output
        assert len(db.get_tasks(project_id=project_id)) == 0

    def test_accepts_with_description(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_path, db, project_id = _setup_db(tmp_path)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        monkeypatch.setattr("pt._detect_project_from_cwd", lambda db: None)

        runner = CliRunner()
        result = runner.invoke(
            tasks_group,
            ["create", "New feature", "-p", project_id, "-d", VALID_NOTES],
        )
        assert "Created task" in result.output
        tasks = db.get_tasks(project_id=project_id)
        assert len(tasks) == 1
        assert tasks[0]["notes"] == VALID_NOTES

    def test_proposal_not_exempt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_path, db, project_id = _setup_db(tmp_path)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        monkeypatch.setattr("pt._detect_project_from_cwd", lambda db: None)

        runner = CliRunner()
        result = runner.invoke(
            tasks_group, ["create", "Proposed idea", "-p", project_id, "--proposal"]
        )
        assert result.exit_code != 0, "a rejected create must not exit 0"
        assert "Acceptance criteria required" in result.output
        assert len(db.get_tasks(project_id=project_id, status=None)) == 0

    def test_help_documents_the_requirement(self):
        runner = CliRunner()
        result = runner.invoke(tasks_group, ["create", "--help"])
        assert result.exit_code == 0
        assert "- [ ] <how completion is verified>" in result.output


# --- Dashboard API: POST /api/tasks ---


class TestDashboardApiCreate:
    def test_rejects_without_notes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_path, db, project_id = _setup_db(tmp_path)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        from dashboard.app import app

        client = TestClient(app)
        response = client.post(
            "/api/tasks", json={"text": "New feature", "project_id": project_id}
        )
        assert response.status_code == 400
        body = response.json()
        assert "Acceptance criteria required" in body["message"]
        assert body["details"]["field"] == "notes"
        assert len(db.get_tasks(project_id=project_id)) == 0

    def test_accepts_with_notes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        db_path, db, project_id = _setup_db(tmp_path)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        from dashboard.app import app

        client = TestClient(app)
        response = client.post(
            "/api/tasks",
            json={"text": "New feature", "project_id": project_id, "notes": VALID_NOTES},
        )
        assert response.status_code == 201
        assert response.json()["notes"] == VALID_NOTES
