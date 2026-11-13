"""`pt tasks` exits non-zero when they did not do what was asked.

Found by the #8093 survey: transitions printed "Failed ..." / "not found" /
"blocked" and exited 0, so scripts and agents read a refused card move (for
example the state machine's "Cannot move from To Do to Done") as success.
"""

import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pt  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402
from pt import tasks_group  # noqa: E402


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    manager = DatabaseManager(db_path)
    manager.add_project(project_id="proj", name="Proj", path="/tmp/proj", status="active")
    return manager


def _run(*args: str):
    return CliRunner().invoke(tasks_group, list(args))


def test_successful_transitions_exit_zero(db) -> None:
    task = db.add_task(text="ok", project_id="proj", status="To Do")
    result = _run("start", str(task["id"]))
    assert result.exit_code == 0, result.output
    assert db.get_task(task["id"])["status"] == "In Progress"
    assert _run("review", str(task["id"])).exit_code == 0
    assert _run("done", str(task["id"])).exit_code == 0
    assert db.get_task(task["id"])["status"] == "Done"


@pytest.mark.parametrize("command", ["start", "review", "done", "cancel", "approve", "reject"])
def test_unknown_card_exits_one(db, command: str) -> None:
    result = _run(command, "999999")
    assert result.exit_code == 1
    assert "not found" in result.output or "Failed" in result.output


def test_state_machine_refusal_exits_one_and_names_it(db) -> None:
    task = db.add_task(text="skip", project_id="proj", status="To Do")
    result = _run("done", str(task["id"]))
    assert result.exit_code == 1
    assert "Cannot move from To Do to Done" in result.output
    assert db.get_task(task["id"])["status"] == "To Do"


def test_partial_batch_does_the_good_ones_but_exits_one(db) -> None:
    good = db.add_task(text="good", project_id="proj", status="Review")
    result = _run("done", str(good["id"]), "999999")
    assert result.exit_code == 1
    assert db.get_task(good["id"])["status"] == "Done"
    assert "Completed 1/2 tasks" in result.output


def test_blocked_start_exits_one(db) -> None:
    blocker = db.add_task(text="blocker", project_id="proj", status="To Do")
    blocked = db.add_task(text="blocked", project_id="proj", status="To Do")
    db.update_task(blocked["id"], blocked_by=str(blocker["id"]))
    result = _run("start", str(blocked["id"]))
    assert result.exit_code == 1
    assert "blocked" in result.output
    assert db.get_task(blocked["id"])["status"] == "To Do"


@pytest.mark.parametrize("command", ["approve", "reject"])
def test_non_proposal_exits_one(db, command: str) -> None:
    task = db.add_task(text="plain", project_id="proj", status="Backlog")
    result = _run(command, str(task["id"]))
    assert result.exit_code == 1
    assert "is not a proposal" in result.output


def test_move_to_unknown_project_exits_one(db) -> None:
    task = db.add_task(text="t", project_id="proj", status="Backlog")
    assert _run("move", "no-such-project", str(task["id"])).exit_code == 1


def test_move_to_its_current_project_is_not_a_failure(db) -> None:
    task = db.add_task(text="t", project_id="proj", status="Backlog")
    result = _run("move", "proj", str(task["id"]))
    assert result.exit_code == 0
    assert "already in project" in result.output


def test_delete_unknown_card_exits_one_but_declining_is_not_a_failure(db) -> None:
    assert _run("delete", "999999", "--yes").exit_code == 1
    task = db.add_task(text="keep me", project_id="proj", status="Backlog")
    result = CliRunner().invoke(tasks_group, ["delete", str(task["id"])], input="n\n")
    assert result.exit_code == 0
    assert db.get_task(task["id"]) is not None


def test_an_exception_while_changing_a_card_exits_one(db, monkeypatch) -> None:
    task = db.add_task(text="t", project_id="proj", status="Review")

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    import db.backend_manager as backend

    monkeypatch.setattr(backend.DatabaseManager, "update_task", boom)
    monkeypatch.setattr(backend.DatabaseManager, "delete_task", boom)
    db.add_project(project_id="other", name="Other", path="/tmp/other", status="active")
    for args, message in (
        (["done", str(task["id"])], "Failed to complete task"),
        (["cancel", str(task["id"])], "Failed to cancel task"),
        (["move", "other", str(task["id"])], "Failed to move task"),  # a real card: reaches update_task
        (["delete", str(task["id"]), "--yes"], "Failed to delete task"),  # reaches delete_task
    ):
        result = _run(*args)
        assert result.exit_code == 1, (args, result.output)
        assert message in result.output, (args, result.output)

