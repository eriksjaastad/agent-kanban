"""`--blocked-by` must resolve display IDs and refuse unknown ones (#6747).

`pt tasks create/update --blocked-by` used to run a raw `int()` over the
tokens and store them verbatim. Two failures fell out of that:

1. Display IDs (what every other command accepts, and what the board prints)
   were stored as-is. They match no `tasks.id`, so the dependency was inert.
2. Nothing checked existence, so a typo was accepted silently.

Either way `get_blocking_tasks` dropped the ID, `is_blocked` answered
``(False, [])``, and `pt tasks show` printed "Blocked by: (all resolved)" —
the exact opposite of the truth — for a card that was genuinely blocked.
"""

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.backend_manager import DatabaseManager as BackendDatabaseManager  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402
from pt import tasks_group  # noqa: E402

PROJECT_ID = "project-tracker"


@pytest.fixture
def backend_db(local_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    """Database manager that CLI subprocess can reach through daemon."""
    db_path = tmp_path / "test.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    # Use Remote manager so both test and CLI subprocess use daemon
    manager = DatabaseManager()
    manager.add_project(
        project_id=PROJECT_ID,
        name="Project Tracker",
        path="/tmp/project-tracker",
        status="active",
    )
    return manager


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _find(db: DatabaseManager, text: str):
    return next(t for t in db.get_tasks(project_id=PROJECT_ID) if t["text"] == text)


def _unused_display_id(db: DatabaseManager) -> int:
    """A small integer that resolves to no task at all."""
    candidate = 999999
    assert db.resolve_task_id(candidate) is None
    return candidate


# ---------------------------------------------------------------------
# create
# ---------------------------------------------------------------------


def test_create_resolves_display_id_to_canonical_pk(backend_db, runner):
    blocker = backend_db.add_task(text="Blocker", project_id=PROJECT_ID)
    display_id = backend_db.get_task_display_id(blocker["id"])
    assert display_id is not None and display_id != blocker["id"]

    result = runner.invoke(
        tasks_group,
        [
            "create", "Blocked card", "-p", PROJECT_ID,
            "--blocked-by", str(display_id),
            "-d", "- [ ] Blocker resolves and this card becomes unblocked",
        ],
    )
    assert result.exit_code == 0, result.output

    created = _find(backend_db, "Blocked card")
    # The PK is stored, not the display ID the operator typed.
    assert json.loads(created["blocked_by"]) == [blocker["id"]]
    # And the dependency is actually live.
    assert backend_db.is_blocked(created["id"]) == (True, [blocker["id"]])


def test_create_refuses_unknown_blocked_by_id(backend_db, runner):
    bogus = _unused_display_id(backend_db)
    result = runner.invoke(
        tasks_group,
        ["create", "Never created", "-p", PROJECT_ID, "--blocked-by", str(bogus)],
    )

    assert result.exit_code != 0
    assert f"#{bogus}" in result.output
    assert not [
        t for t in backend_db.get_tasks(project_id=PROJECT_ID) if t["text"] == "Never created"
    ]


def test_create_refuses_when_only_one_of_several_ids_is_bad(backend_db, runner):
    blocker = backend_db.add_task(text="Blocker", project_id=PROJECT_ID)
    bogus = _unused_display_id(backend_db)
    result = runner.invoke(
        tasks_group,
        [
            "create", "Never created", "-p", PROJECT_ID,
            "--blocked-by", f"{backend_db.get_task_display_id(blocker['id'])},{bogus}",
        ],
    )

    assert result.exit_code != 0
    assert f"#{bogus}" in result.output


def test_create_rejects_non_numeric_token(backend_db, runner):
    result = runner.invoke(
        tasks_group,
        ["create", "Never created", "-p", PROJECT_ID, "--blocked-by", "abc"],
    )
    assert result.exit_code != 0
    assert "comma-separated task IDs" in result.output


# ---------------------------------------------------------------------
# update
# ---------------------------------------------------------------------


def test_update_resolves_display_id_to_canonical_pk(backend_db, runner):
    blocker = backend_db.add_task(text="Blocker", project_id=PROJECT_ID)
    target = backend_db.add_task(text="Target", project_id=PROJECT_ID)
    blocker_display = backend_db.get_task_display_id(blocker["id"])
    target_display = backend_db.get_task_display_id(target["id"])

    result = runner.invoke(
        tasks_group,
        ["update", str(target_display), "--blocked-by", str(blocker_display)],
    )
    assert result.exit_code == 0, result.output

    stored = backend_db.get_task(target["id"])
    assert json.loads(stored["blocked_by"]) == [blocker["id"]]
    assert backend_db.is_blocked(target["id"]) == (True, [blocker["id"]])


def test_update_refuses_unknown_blocked_by_id(backend_db, runner):
    target = backend_db.add_task(text="Target", project_id=PROJECT_ID)
    bogus = _unused_display_id(backend_db)

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(target["id"])),
         "--blocked-by", str(bogus)],
    )

    assert result.exit_code != 0
    assert f"#{bogus}" in result.output
    assert backend_db.get_task(target["id"])["blocked_by"] is None


def test_update_can_still_clear_blocked_by(backend_db, runner):
    blocker = backend_db.add_task(text="Blocker", project_id=PROJECT_ID)
    target = backend_db.add_task(
        text="Target", project_id=PROJECT_ID, blocked_by=[blocker["id"]]
    )

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(target["id"])), "--blocked-by", ""],
    )
    assert result.exit_code == 0, result.output
    assert backend_db.get_task(target["id"])["blocked_by"] is None


# ---------------------------------------------------------------------
# show — the line that used to read as its own opposite
# ---------------------------------------------------------------------


def _force_blocked_by(db: DatabaseManager, task_id: int, value: str) -> None:
    """Write blocked_by past the CLI, simulating pre-fix rows in the DB."""
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET blocked_by = ? WHERE id = ?", (value, task_id)
        )
        conn.commit()


def test_show_flags_stored_ids_that_resolve_to_nothing(backend_db, runner):
    target = backend_db.add_task(text="Target", project_id=PROJECT_ID)
    bogus = _unused_display_id(backend_db)
    _force_blocked_by(backend_db, target["id"], json.dumps([bogus]))

    result = runner.invoke(
        tasks_group, ["show", str(backend_db.get_task_display_id(target["id"]))]
    )
    assert result.exit_code == 0, result.output
    assert "UNRESOLVED" in result.output
    assert f"#{bogus}" in result.output
    assert "all resolved" not in result.output


def test_show_says_all_resolved_only_when_blockers_are_really_done(backend_db, runner):
    blocker = backend_db.add_task(text="Blocker", project_id=PROJECT_ID, status="Done")
    target = backend_db.add_task(
        text="Target", project_id=PROJECT_ID, blocked_by=[blocker["id"]]
    )

    result = runner.invoke(
        tasks_group, ["show", str(backend_db.get_task_display_id(target["id"]))]
    )
    assert result.exit_code == 0, result.output
    assert "Blocked by: (all resolved)" in result.output


def test_show_reports_incomplete_blockers(backend_db, runner):
    blocker = backend_db.add_task(text="Blocker", project_id=PROJECT_ID, status="Backlog")
    target = backend_db.add_task(
        text="Target", project_id=PROJECT_ID, blocked_by=[blocker["id"]]
    )

    result = runner.invoke(
        tasks_group, ["show", str(backend_db.get_task_display_id(target["id"]))]
    )
    assert result.exit_code == 0, result.output
    assert "(incomplete)" in result.output
    assert "all resolved" not in result.output


# ---------------------------------------------------------------------
# tree — resolved its subject nowhere before this
# ---------------------------------------------------------------------


def test_tree_accepts_a_display_id(backend_db, runner):
    parent = backend_db.add_task(text="Parent", project_id=PROJECT_ID)
    backend_db.add_task(text="Child", project_id=PROJECT_ID, parent_id=parent["id"])
    display_id = backend_db.get_task_display_id(parent["id"])
    assert display_id != parent["id"]

    result = runner.invoke(tasks_group, ["tree", str(display_id)])
    assert result.exit_code == 0, result.output
    assert "not found" not in result.output
    assert "Child" in result.output
