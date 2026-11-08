"""Tests for `pt handoff` command group (Phase D: structured unfinished-work records).

Schema version: pt.handoff.v1

Uses CliRunner (Click 8.3+, no mix_stderr kwarg).
Each test sets PT_DB_PATH to a tmp_path-based DB and applies the real
migration (010_add_handoffs_table.py) via the migration runner — no
parallel DDL string is maintained inside the test suite.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from db.manager import DatabaseManager  # noqa: E402
from pt import cli  # noqa: E402 — path setup precedes import


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = SCRIPTS_DIR / "db" / "migrations"


def _make_tracker_db() -> None:
    """Seed card 6151 into the isolated local database."""
    DatabaseManager().seed_task(
        6151,
        text="Phase D handoff test card",
        project_id="project-tracker",
        status="In Progress",
        created_at="2026-05-05T00:00:00Z",
    )


_COMMON_ENV = {
    "PT_SUPPRESS_MIGRATION_WARNING": "1",
    "PT_NO_BANNER": "1",
}

_REQUIRED_CREATE_ARGS = [
    "--intent", "Add pt handoff command for unfinished-work records",
    "--status", "Migration written, command scaffolded, tests pending",
    "--next", "pytest tests/test_handoff_cli.py -v",
    "--guidance", "Keep all staged files; discard nothing",
]


def _invoke(tmp_path: Path, args: list[str]):
    """Seed the card, then run `pt` against this test's own database."""
    _make_tracker_db()
    return _invoke_on_db(None, args)


def _invoke_on_db(db_path, args: list[str]):
    """Invoke the CLI against the per-test database selected by PT_DB_PATH."""
    runner = CliRunner()
    return runner.invoke(cli, args, env=dict(_COMMON_ENV), catch_exceptions=False)


# ---------------------------------------------------------------------------
# create — happy path
# ---------------------------------------------------------------------------

def test_handoff_create_unfinished_json(tmp_path: Path) -> None:
    """create with --type unfinished emits pt.handoff.v1 JSON."""
    result = _invoke(
        tmp_path,
        ["handoff", "create", "6151", "--json", "--type", "unfinished"] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "pt.handoff.v1"
    assert payload["ok"] is True
    assert payload["command"] == "handoff.create"
    rec = payload["result"]
    assert rec["card_id"] == 6151
    assert rec["record_type"] == "unfinished"
    assert rec["project"] == "project-tracker"
    assert rec["resolved_at"] is None
    assert isinstance(rec["file_list"], list)


def test_handoff_create_persists_record(tmp_path: Path) -> None:
    """create inserts a row visible via handoff list."""
    db_path = None
    _make_tracker_db()
    _invoke_on_db(db_path, ["handoff", "create", "6151"] + _REQUIRED_CREATE_ARGS)
    result = _invoke_on_db(db_path, ["handoff", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["result"]) == 1
    assert payload["result"][0]["card_id"] == 6151


def test_handoff_create_with_auto_files(tmp_path: Path) -> None:
    """--auto-files runs git status; result is a list (may be empty in test env)."""
    result = _invoke(
        tmp_path,
        ["handoff", "create", "6151", "--json", "--auto-files"] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload["result"]["file_list"], list)


def test_handoff_create_with_explicit_files(tmp_path: Path) -> None:
    """--files accepts a JSON array and round-trips it."""
    files_json = json.dumps([
        {"path": "scripts/pt.py", "classification": "dirty"},
        {"path": "tests/test_handoff_cli.py", "classification": "untracked"},
    ])
    result = _invoke(
        tmp_path,
        ["handoff", "create", "6151", "--json", "--files", files_json] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    fl = payload["result"]["file_list"]
    assert len(fl) == 2
    assert fl[0]["path"] == "scripts/pt.py"
    assert fl[1]["classification"] == "untracked"


# ---------------------------------------------------------------------------
# create — pr_exempt happy path
# ---------------------------------------------------------------------------

def test_handoff_create_pr_exempt_json(tmp_path: Path) -> None:
    """create with --type pr_exempt emits correct JSON envelope."""
    result = _invoke(
        tmp_path,
        [
            "handoff", "create", "6151",
            "--json",
            "--type", "pr_exempt",
            "--reason", "Changes reverted by rebase; nothing to ship",
            "--disposition", "reverted",
            "--approver", "erie-login",
        ] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    rec = payload["result"]
    assert rec["record_type"] == "pr_exempt"
    assert rec["pr_exempt_reason"] == "Changes reverted by rebase; nothing to ship"
    assert rec["pr_exempt_disposition"] == "reverted"
    assert rec["pr_exempt_approver"] == "erie-login"


# ---------------------------------------------------------------------------
# create — validation errors
# ---------------------------------------------------------------------------

def test_handoff_create_pr_exempt_missing_reason(tmp_path: Path) -> None:
    """pr_exempt without --reason exits with EXIT_VALIDATION=2."""
    result = _invoke(
        tmp_path,
        [
            "handoff", "create", "6151",
            "--json",
            "--type", "pr_exempt",
            "--disposition", "discarded",
        ] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "reason" in payload["error"]["message"].lower()


def test_handoff_create_pr_exempt_missing_disposition(tmp_path: Path) -> None:
    """pr_exempt without --disposition exits with EXIT_VALIDATION=2."""
    result = _invoke(
        tmp_path,
        [
            "handoff", "create", "6151",
            "--json",
            "--type", "pr_exempt",
            "--reason", "Nothing to ship",
        ] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"


def test_handoff_create_invalid_card_id(tmp_path: Path) -> None:
    """Non-existent card_id exits with EXIT_VALIDATION=2 and structured error."""
    result = _invoke(
        tmp_path,
        ["handoff", "create", "9999999", "--json"] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "9999999" in payload["error"]["message"]


def test_handoff_create_files_and_auto_files_exclusive(tmp_path: Path) -> None:
    """--files and --auto-files together exit with EXIT_VALIDATION=2."""
    result = _invoke(
        tmp_path,
        [
            "handoff", "create", "6151", "--json",
            "--files", "[]",
            "--auto-files",
        ] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"


# ---------------------------------------------------------------------------
# list — filters
# ---------------------------------------------------------------------------

def _seed_two_handoffs(db_path: Path) -> None:
    """Insert two handoffs: one for card 6151 (project-tracker), one won't match filters."""
    runner = CliRunner()
    env = {**_COMMON_ENV, "PT_DB_PATH": str(db_path or os.environ["PT_DB_PATH"])}
    # First handoff for card 6151
    runner.invoke(
        cli,
        ["handoff", "create", "6151"] + _REQUIRED_CREATE_ARGS,
        env=env,
        catch_exceptions=False,
    )
    # Second handoff for card 6151 (different intent, will also match card filter)
    runner.invoke(
        cli,
        [
            "handoff", "create", "6151",
            "--intent", "Second handoff intent",
            "--status", "nothing done",
            "--next", "echo done",
            "--guidance", "discard all",
        ],
        env=env,
        catch_exceptions=False,
    )


def test_handoff_list_json_schema(tmp_path: Path) -> None:
    """list --json returns pt.handoff.v1 envelope with a list result."""
    db_path = None
    _make_tracker_db()
    _seed_two_handoffs(db_path)
    result = _invoke_on_db(db_path, ["handoff", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "pt.handoff.v1"
    assert payload["ok"] is True
    assert payload["command"] == "handoff.list"
    assert isinstance(payload["result"], list)
    assert len(payload["result"]) == 2


def test_handoff_list_filter_by_card(tmp_path: Path) -> None:
    """--card filters to only matching records."""
    db_path = None
    _make_tracker_db()
    _seed_two_handoffs(db_path)
    result = _invoke_on_db(db_path, ["handoff", "list", "--card", "6151", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for rec in payload["result"]:
        assert rec["card_id"] == 6151


def test_handoff_list_filter_by_project(tmp_path: Path) -> None:
    """--project filters to only matching records."""
    db_path = None
    _make_tracker_db()
    _seed_two_handoffs(db_path)
    result = _invoke_on_db(db_path, ["handoff", "list", "--project", "project-tracker", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["result"]) >= 1
    for rec in payload["result"]:
        assert rec["project"] == "project-tracker"


def test_handoff_list_unresolved_only(tmp_path: Path) -> None:
    """--unresolved-only excludes resolved records."""
    db_path = None
    _make_tracker_db()
    _seed_two_handoffs(db_path)

    # Resolve the first handoff (id=1)
    _invoke_on_db(db_path, ["handoff", "resolve", "1", "--note", "done"])

    result = _invoke_on_db(db_path, ["handoff", "list", "--unresolved-only", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for rec in payload["result"]:
        assert rec["resolved_at"] is None


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def test_handoff_show_json(tmp_path: Path) -> None:
    """show HANDOFF_ID --json returns single record with pt.handoff.v1 envelope."""
    db_path = None
    _make_tracker_db()
    # Create one handoff
    create_result = _invoke_on_db(
        db_path,
        ["handoff", "create", "6151", "--json"] + _REQUIRED_CREATE_ARGS,
    )
    handoff_id = json.loads(create_result.output)["result"]["id"]

    result = _invoke_on_db(db_path, ["handoff", "show", str(handoff_id), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "pt.handoff.v1"
    assert payload["ok"] is True
    assert payload["command"] == "handoff.show"
    assert payload["result"]["id"] == handoff_id
    assert payload["result"]["card_id"] == 6151


def test_handoff_show_missing_id(tmp_path: Path) -> None:
    """show on non-existent handoff ID exits with EXIT_VALIDATION=2."""
    result = _invoke(tmp_path, ["handoff", "show", "99999", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def test_handoff_resolve_sets_resolved_at(tmp_path: Path) -> None:
    """resolve sets resolved_at and resolved_note on the record."""
    db_path = None
    _make_tracker_db()
    create_result = _invoke_on_db(
        db_path,
        ["handoff", "create", "6151", "--json"] + _REQUIRED_CREATE_ARGS,
    )
    handoff_id = json.loads(create_result.output)["result"]["id"]

    result = _invoke_on_db(
        db_path,
        ["handoff", "resolve", str(handoff_id), "--note", "Resumed and completed", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "pt.handoff.v1"
    assert payload["ok"] is True
    assert payload["command"] == "handoff.resolve"
    rec = payload["result"]
    assert rec["resolved_at"] is not None
    assert rec["resolved_note"] == "Resumed and completed"


def test_handoff_resolve_missing_id(tmp_path: Path) -> None:
    """resolve on non-existent handoff ID exits with EXIT_VALIDATION=2."""
    result = _invoke(tmp_path, ["handoff", "resolve", "99999", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"


# ---------------------------------------------------------------------------
# JSON envelope — error path
# ---------------------------------------------------------------------------

def test_handoff_error_path_emits_ok_false(tmp_path: Path) -> None:
    """All JSON error paths emit ok=false with an error.class field."""
    result = _invoke(tmp_path, ["handoff", "create", "9999999", "--json"] + _REQUIRED_CREATE_ARGS)
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "class" in payload["error"]
    assert "message" in payload["error"]


# ---------------------------------------------------------------------------
# A git failure is an error, never a guessed-empty branch or file list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [["--auto-files", "--branch", "b"], []], ids=["auto_files", "branch"])
@pytest.mark.parametrize("failure, message", [
    (FileNotFoundError("git not found"), "git not found"),
    (subprocess.TimeoutExpired(cmd="git", timeout=10), "timed out"),
    (subprocess.CompletedProcess([], 128, "", "fatal: not a git repository"), "exited 128: fatal: not a git"),
], ids=["missing", "timeout", "nonzero"])
def test_a_git_failure_records_nothing(monkeypatch, args, failure, message) -> None:
    import pt as pt_module

    _make_tracker_db()

    def fake_run(*_args, **_kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(pt_module.subprocess, "run", fake_run)
    result = _invoke_on_db(None, ["handoff", "create", "6151", "--json", *args] + _REQUIRED_CREATE_ARGS)
    payload = json.loads(result.output)
    assert result.exit_code != 0 and payload["ok"] is False
    assert payload["error"]["class"] == "query_failure" and message in payload["error"]["message"]
    listed = json.loads(_invoke_on_db(None, ["handoff", "list", "--json"]).output)["result"]
    assert listed == []


# ---------------------------------------------------------------------------
# LOW #3 — --files element-shape validation
# ---------------------------------------------------------------------------

def test_handoff_create_files_invalid_element_type(tmp_path: Path) -> None:
    """--files element that isn't a dict raises validation error."""
    files_json = json.dumps([1, 2, 3])
    result = _invoke(
        tmp_path,
        ["handoff", "create", "6151", "--json", "--files", files_json] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "element 0" in payload["error"]["message"]


def test_handoff_create_files_missing_required_keys(tmp_path: Path) -> None:
    """--files element missing 'classification' raises validation error."""
    files_json = json.dumps([{"path": "scripts/pt.py"}])
    result = _invoke(
        tmp_path,
        ["handoff", "create", "6151", "--json", "--files", files_json] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "missing required keys" in payload["error"]["message"]


def test_handoff_create_files_unknown_classification(tmp_path: Path) -> None:
    """--files element with classification outside the allowed set is rejected."""
    files_json = json.dumps(
        [{"path": "scripts/pt.py", "classification": "bogus"}]
    )
    result = _invoke(
        tmp_path,
        ["handoff", "create", "6151", "--json", "--files", files_json] + _REQUIRED_CREATE_ARGS,
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "classification" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# LOW #4 — double-resolve idempotency (refuse to overwrite)
# ---------------------------------------------------------------------------

def test_handoff_resolve_double_refuses(tmp_path: Path) -> None:
    """Resolving an already-resolved handoff raises a validation error and
    does NOT overwrite the original resolved_at."""
    db_path = None
    _make_tracker_db()
    create_result = _invoke_on_db(
        db_path,
        ["handoff", "create", "6151", "--json"] + _REQUIRED_CREATE_ARGS,
    )
    handoff_id = json.loads(create_result.output)["result"]["id"]

    first = _invoke_on_db(
        db_path,
        ["handoff", "resolve", str(handoff_id), "--note", "first", "--json"],
    )
    assert first.exit_code == 0, first.output
    first_resolved_at = json.loads(first.output)["result"]["resolved_at"]
    assert first_resolved_at is not None

    second = _invoke_on_db(
        db_path,
        ["handoff", "resolve", str(handoff_id), "--note", "second", "--json"],
    )
    assert second.exit_code == 2, second.output
    payload = json.loads(second.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "already resolved" in payload["error"]["message"]

    # Verify the show output still reflects the FIRST resolution.
    show_result = _invoke_on_db(db_path, ["handoff", "show", str(handoff_id), "--json"])
    assert show_result.exit_code == 0, show_result.output
    show_payload = json.loads(show_result.output)
    assert show_payload["result"]["resolved_at"] == first_resolved_at
    assert show_payload["result"]["resolved_note"] == "first"
