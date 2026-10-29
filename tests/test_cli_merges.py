"""#8093 PR 8b: duplicate `pt` front ends merged into one implementation each.

Each pair of spellings must run the same code and agree on output; hidden
aliases stay out of --help; the read-only `pt hygiene` scan stays a plain
command, separate from the destructive `pt worktrees clean`.
"""

import json
import sys
from pathlib import Path

import click
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
    monkeypatch.setenv("PT_CALLER_CWD", str(tmp_path))
    monkeypatch.setenv("PT_NO_BANNER", "1")
    manager = DatabaseManager(db_path)
    manager.add_project(project_id="proj-id", name="Proj Name", path="/tmp/proj", status="active")
    manager.add_task(text="open one", project_id="proj-id", status="To Do")
    manager.add_task(text="working", project_id="proj-id", status="In Progress")
    manager.add_task(text="finished", project_id="proj-id", status="Done")
    return manager


def _run(args):
    return CliRunner().invoke(pt.cli, args)


# --- pt tasks == pt tasks list ------------------------------------------------

TASK_ARGS = [
    [],
    ["-p", "proj-id"],
    ["-p", "proj-id", "--json"],
    ["-p", "proj-id", "--all"],
    ["-p", "proj-id", "-s", "Done", "--json"],
    ["-p", "proj-id", "--needs-prompt", "--json"],
    ["-p", "proj-id", "--ready"],
    ["-p", "proj-id", "--proposals", "--json"],
    ["-p", "proj-id", "--archived"],
    ["-p", "proj-id", "--board"],
    ["-p", "missing"],
]


@pytest.mark.parametrize("args", TASK_ARGS)
def test_bare_tasks_and_tasks_list_agree(db, args) -> None:
    bare = _run(["tasks", *args])
    listed = _run(["tasks", "list", *args])
    assert bare.exit_code == listed.exit_code == 0, (bare.output, listed.output)
    assert bare.output == listed.output
    assert bare.output.strip()


def test_tasks_json_shape_matches_and_parses(db) -> None:
    bare = json.loads(_run(["tasks", "-p", "proj-id", "--json"]).output)
    listed = json.loads(_run(["tasks", "list", "-p", "proj-id", "--json"]).output)
    assert bare == listed
    texts = {t["text"] for t in (bare["tasks"] if isinstance(bare, dict) else bare)}
    assert texts == {"open one", "working"}


def test_both_tasks_spellings_run_one_implementation(db, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(pt, "_tasks_list_impl", lambda *a: calls.append(a))
    _run(["tasks", "-p", "proj-id", "--board", "--all"])
    _run(["tasks", "list", "-p", "proj-id", "--board", "--all"])
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0] == "proj-id" and calls[0][2] is True and calls[0][3] is True


def test_tasks_and_list_share_the_same_options() -> None:
    def names(cmd):
        return [(p.name, tuple(p.opts), p.default, p.help) for p in cmd.params]

    listed = pt.tasks_group.commands["list"]
    assert names(pt.tasks_group) == names(listed)


# --- pt scan / pt refresh -----------------------------------------------------

def test_scan_and_refresh_are_one_command(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(pt, "_scan_impl", lambda **kw: seen.append(kw))
    flags = ["--no-graph", "--dry-run", "--force"]
    assert _run(["scan", *flags]).exit_code == 0
    assert _run(["refresh", *flags]).exit_code == 0
    assert _run(["scan"]).exit_code == 0
    assert _run(["refresh"]).exit_code == 0
    assert seen[0] == seen[1] == {"no_graph": True, "dry_run": True, "force": True}
    assert seen[2] == seen[3] == {"no_graph": False, "dry_run": False, "force": False}
    assert pt.cli.commands["refresh"].callback is pt.cli.commands["scan"].callback


def test_refresh_is_hidden_scan_is_not() -> None:
    out = _run(["--help"]).output
    assert "scan " in out
    assert "refresh" not in out


# --- pt help ------------------------------------------------------------------

def test_help_is_clicks_own_help_and_hidden() -> None:
    top = _run(["--help"])
    assert _run(["help"]).output == top.output
    assert not any(line.strip().startswith("help ") for line in top.output.splitlines())
    assert _run(["help", "tasks"]).output == _run(["tasks", "--help"]).output
    assert _run(["help", "tasks", "list"]).output == _run(["tasks", "list", "--help"]).output


def test_help_unknown_command_is_a_usage_error() -> None:
    result = _run(["help", "nope"])
    assert result.exit_code == 2
    assert "No such command 'nope'" in result.output


# --- usage lives in --help ----------------------------------------------------

def test_hygiene_and_scan_help_carry_their_usage_contract() -> None:
    """The hygiene and memory JSON contracts and scan usage live in --help."""
    hygiene_help = CliRunner().invoke(pt.cli, ["hygiene", "--help"]).output
    for needle in ("Read-only", "pt.hygiene.v1", "exit 6", '"error"', "findings"):
        assert needle in hygiene_help, needle
    # Click rewraps help to the terminal width, so match on collapsed whitespace.
    memory_help = " ".join(CliRunner().invoke(pt.cli, ["memory", "--help"]).output.split())
    for needle in ("schema_version", "read_only", "NDJSON", "exit 2", "Option parsing errors"):
        assert needle in memory_help, needle
    assert "sync-project" in CliRunner().invoke(pt.cli, ["scan", "--help"]).output


def test_hygiene_stays_a_plain_read_only_command() -> None:
    """Destructive worktree cleanup is not folded under the read-only scan."""
    assert not isinstance(pt.cli.commands["hygiene"], click.Group)
    assert "worktrees" in pt.cli.commands and not pt.cli.commands["worktrees"].hidden
