"""#8093 PRs 1 and 4: unused commands are gone, `status` takes an id, and the shared
card-transition loop keeps its contract (a skip is neither success nor failure).
"""

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pt  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    manager = DatabaseManager(db_path)
    manager.add_project(project_id="proj-id", name="Proj Name", path="/tmp/proj", status="active")
    manager.add_project(project_id="other", name="Other", path="/tmp/other", status="active")
    return manager


@pytest.mark.parametrize("name", ["proj-id", "PROJ NAME", "Proj Name"])
def test_status_accepts_id_or_name(db, name: str) -> None:
    result = CliRunner().invoke(pt.cli, ["status", name])
    assert result.exit_code == 0, result.output
    assert "Proj Name" in result.output
    assert "not found" not in result.output


def test_status_unknown_project_says_not_found(db) -> None:
    result = CliRunner().invoke(pt.cli, ["status", "nope"])
    assert "Project 'nope' not found" in result.output


def test_move_skip_is_not_a_failure(db) -> None:
    a = db.add_task(text="already there", project_id="other", status="To Do")
    b = db.add_task(text="moves", project_id="proj-id", status="To Do")
    result = CliRunner().invoke(pt.tasks_group, ["move", "other", str(a["id"]), str(b["id"])])
    assert result.exit_code == 0, result.output
    assert f"Task #{a['id']} already in project 'Other'" in result.output
    assert "Moved 1/2 tasks to 'Other'" in result.output
    assert db.get_task(b["id"])["project_id"] == "other"


def test_mixed_batch_keeps_going_then_exits_one(db) -> None:
    ok = db.add_task(text="fine", project_id="proj-id", status="To Do")
    result = CliRunner().invoke(pt.tasks_group, ["start", "999999", str(ok["id"])])
    assert result.exit_code == 1
    assert "Task #999999 not found" in result.output
    assert f"Started: #{ok['id']} - fine" in result.output
    assert "Started 1/2 tasks" in result.output
    assert db.get_task(ok["id"])["status"] == "In Progress"


def test_approve_refuses_non_proposal(db) -> None:
    t = db.add_task(text="plain", project_id="proj-id", status="To Do")
    result = CliRunner().invoke(pt.tasks_group, ["approve", str(t["id"])])
    assert result.exit_code == 1
    assert f"Task #{t['id']} is not a proposal (type: " in result.output


@pytest.fixture
def no_portfolio_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    # retire-project's dry run greps ~/projects for references; not under test.
    monkeypatch.setattr(pt, "_scan_portfolio_for_references", lambda *a: ([], []))


@pytest.mark.parametrize("token", ["proj-id", "PROJ-ID", "proj name"])
def test_retire_project_resolves_id_or_name(db, no_portfolio_scan, token: str) -> None:
    result = CliRunner().invoke(pt.cli, ["retire-project", token])
    assert result.exit_code == 0, result.output
    assert "Proj Name (proj-id)" in result.output
    assert "Dry run complete" in result.output


@pytest.mark.parametrize("name_match_first", [True, False])
def test_an_id_beats_another_projects_name(db, no_portfolio_scan, monkeypatch, name_match_first) -> None:
    # Before #8093 `status` matched names only and retire-project took the
    # first row matching either, so the winner depended on row order.
    from db import backend_manager

    by_id = {"id": "x", "name": "First", "path": "/tmp/first", "status": "active"}
    by_name = {"id": "y", "name": "x", "path": "/tmp/second", "status": "active"}
    rows = [by_name, by_id] if name_match_first else [by_id, by_name]
    monkeypatch.setattr(backend_manager.DatabaseManager, "get_all_projects", lambda self, *a, **k: rows)
    assert pt._resolve_project_id(DatabaseManager(), "X") == "x"

    db.add_project(project_id="x", name="First", path="/tmp/first", status="active")
    db.add_project(project_id="y", name="x", path="/tmp/second", status="active")
    status = CliRunner().invoke(pt.cli, ["status", "x"])
    assert status.exit_code == 0, status.output
    assert "First" in status.output
    retire = CliRunner().invoke(pt.cli, ["retire-project", "x"])
    assert "First (x)" in retire.output


@pytest.mark.parametrize("command", [["status", "Dup"], ["retire-project", "dup"]])
def test_a_shared_name_is_refused_not_guessed(db, no_portfolio_scan, monkeypatch, command) -> None:
    # add_project refuses a duplicate name, but its check races; simulate the
    # two rows a concurrent add could leave.
    from db import backend_manager

    rows = [
        {"id": "dup-1", "name": "Dup", "path": "/tmp/d1", "status": "active"},
        {"id": "dup-2", "name": "Dup", "path": "/tmp/d2", "status": "active"},
    ]
    monkeypatch.setattr(backend_manager.DatabaseManager, "get_all_projects", lambda self, *a, **k: rows)
    result = CliRunner().invoke(pt.cli, command)
    assert result.exit_code == 2
    assert "matches 2 projects" in result.output
    assert "dup-1" in result.output and "dup-2" in result.output
    assert "DRY RUN" not in result.output


def test_done_prints_summary_then_tip_then_exits_one(db) -> None:
    ok = db.add_task(text="finish me", project_id="proj-id", status="Review")
    result = CliRunner().invoke(pt.tasks_group, ["done", str(ok["id"]), "999999"])
    assert result.exit_code == 1
    out = result.output
    assert out.index(f"Done: #{ok['id']}") < out.index("Task #999999 not found")
    assert out.index("Task #999999 not found") < out.index("Completed 1/2 tasks")
    assert out.index("Completed 1/2 tasks") < out.index("Tip: Run /compound")
    assert db.get_task(ok["id"])["status"] == "Done"


def test_a_failing_id_lookup_names_the_token_and_exits_one(db, monkeypatch) -> None:
    from db import backend_manager

    def boom(self, token):
        raise RuntimeError("lookup broke")

    monkeypatch.setattr(backend_manager.DatabaseManager, "resolve_task_id", boom)
    result = CliRunner().invoke(pt.tasks_group, ["review", "4242"])
    assert result.exit_code == 1
    assert "Failed to review task #4242: lookup broke" in result.output


@pytest.mark.parametrize("dirname", ["proj-id", "Proj Name"])
def test_tasks_list_detects_project_from_cwd(db, tmp_path, monkeypatch, dirname: str) -> None:
    db.add_task(text="mine", project_id="proj-id", status="To Do")
    db.add_task(text="theirs", project_id="other", status="To Do")
    monkeypatch.setenv("PT_CALLER_CWD", str(tmp_path / dirname))
    result = CliRunner().invoke(pt.tasks_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "mine" in result.output
    assert "theirs" not in result.output


def test_cwd_detection_names_the_directory_when_the_name_is_shared(db, tmp_path, monkeypatch) -> None:
    from db import backend_manager

    rows = [
        {"id": "dup-1", "name": "Dup", "path": "/tmp/d1", "status": "active"},
        {"id": "dup-2", "name": "Dup", "path": "/tmp/d2", "status": "active"},
    ]
    monkeypatch.setattr(backend_manager.DatabaseManager, "get_all_projects", lambda self, *a, **k: rows)
    monkeypatch.setenv("PT_CALLER_CWD", str(tmp_path / "dup"))
    result = CliRunner().invoke(pt.tasks_group, ["list"])
    assert result.exit_code == 2
    assert "Can't tell the project from directory 'dup'" in result.output
    assert "Pass -p <id>" in result.output


def test_graph_keeps_its_live_wrappers_and_drops_the_three_memory_store_cut() -> None:
    """ai-metrics removed `brain.py graph communities|wiki|extract-upgrade`
    (the owner's item-6 mark); pt keeps every other graph wrapper (mark 46)."""
    assert set(pt.graph_group.commands) == {"build", "export", "find", "path", "query", "stats"}
