"""Pin the card-prompt workflow footer.

`pt tasks create --prompt` staples a Workflow Protocol checklist onto every
card prompt. Nothing used to test that text, and it drifted unnoticed for seven
months: it told agents `Awaiting Conductor sign-off` and
``FORBIDDEN: `./pt tasks done` (Conductor only)``, naming a role that is defined
nowhere in the portfolio and forbidding the completion step the Kanban rules
require. That reached 168 cards across 34 projects before anyone noticed, and
the only reason it was found is that a floor manager obeyed it and stalled.

These tests exist so the next drift fails CI instead of shipping. The footer is
asserted verbatim, not by keyword, because the failure mode was wording -- a
substring check for "Workflow Protocol" would have passed the whole time.

The four steps mirror the canonical card walk in the `## Kanban Rules` section
of the portfolio `CLAUDE.md`. If that walk changes, change EXPECTED_FOOTER in
the same commit and say so in the message.
"""

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.backend_manager import DatabaseManager as BackendDatabaseManager
from db.manager import DatabaseManager
from db.schema import create_database
from pt import tasks_group

EXPECTED_FOOTER = (
    "\n---\n\n"
    "## Workflow Protocol\n"
    "- [ ] Start: `pt tasks start <id>`\n"
    "- [ ] Complete work\n"
    "- [ ] Open PR: `pt tasks review <id>`\n"
    "- [ ] PR merges: `pt tasks done <id>`"
)

# Wording that must never reappear. Each entry is something the footer actually
# said at some point, not a hypothetical.
BANNED = [
    "Conductor",       # a role defined nowhere in the portfolio
    "FORBIDDEN",       # forbade the completion step the Kanban rules require
    "Awaiting",        # "Work complete. Awaiting <someone> sign-off."
    "sign-off",
    "./pt",            # predates the global `pt` launcher
]

PROJECT_ID = "project-tracker"


def _setup_db(tmp_path: Path) -> tuple[Path, DatabaseManager]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    # Use the same explicit local database in the fixture and CLI.
    db = DatabaseManager(db_path)
    db.add_project(
        project_id=PROJECT_ID,
        name="Project Tracker",
        path="/tmp/project-tracker",
        status="active",
    )
    return db_path, db


def _create(runner: CliRunner, args: list[str]):
    # #7608: pt tasks create requires an acceptance-criteria checklist line
    # in -d/--description. Not what this module is testing, so supply one
    # by default unless the caller already passed -d/--description.
    if "-d" not in args and "--description" not in args:
        args = [*args, "-d", "- [ ] Footer behavior is covered by this test"]
    result = runner.invoke(tasks_group, ["create", *args])
    assert result.exit_code == 0, f"create failed: {result.output}\n{result.exception}"
    return result


def _only_task(db: DatabaseManager) -> dict:
    tasks = db.get_tasks(project_id=PROJECT_ID)
    assert len(tasks) == 1, f"expected exactly one task, got {len(tasks)}"
    task = db.get_task(tasks[0]["id"])
    assert task is not None, f"task {tasks[0]['id']} vanished between list and get"
    return task


def test_footer_is_appended_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The stored prompt ends with EXPECTED_FOOTER, byte for byte."""
    db_path, db = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    _create(
        CliRunner(),
        ["Footer pinning card", "-p", PROJECT_ID, "--prompt", "Do the thing."],
    )

    prompt = _only_task(db)["prompt"]
    assert prompt is not None
    assert prompt.endswith(EXPECTED_FOOTER), (
        "The workflow footer changed. If this was deliberate, update "
        "EXPECTED_FOOTER in this file in the same commit.\n"
        f"--- got (tail) ---\n{prompt[-400:]!r}\n"
        f"--- expected (tail) ---\n{EXPECTED_FOOTER!r}"
    )
    assert prompt.startswith("Do the thing."), "the author's own prompt must be preserved"


def test_footer_contains_no_banned_wording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No human-gate or pre-launcher wording, whatever else the footer says."""
    db_path, db = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    _create(
        CliRunner(),
        ["Banned wording card", "-p", PROJECT_ID, "--prompt", "Do the thing."],
    )

    prompt = _only_task(db)["prompt"]
    for banned in BANNED:
        assert banned not in prompt, (
            f"{banned!r} is back in the card prompt footer. This wording gated card "
            "completion on an undefined role and reached 168 cards before it was "
            "caught. See the module docstring."
        )


def test_footer_states_the_full_card_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every state transition an agent needs is named.

    Deleting the bad lines without adding these left a checklist that stopped at
    'Complete work' and never said how to reach Done.
    """
    db_path, db = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    _create(
        CliRunner(),
        ["Card walk card", "-p", PROJECT_ID, "--prompt", "Do the thing."],
    )

    prompt = _only_task(db)["prompt"]
    for command in ("pt tasks start", "pt tasks review", "pt tasks done"):
        assert f"`{command} <id>`" in prompt, f"the footer no longer tells the agent to run {command!r}"


def test_no_footer_without_a_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A card created without --prompt gets no footer at all."""
    db_path, db = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    _create(CliRunner(), ["No prompt card", "-p", PROJECT_ID])

    prompt = _only_task(db)["prompt"]
    assert not prompt, f"expected no prompt, got {prompt!r}"
