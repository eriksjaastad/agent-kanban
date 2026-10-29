"""Tests for `pt migration` command group (Phase F: bulk-operation manifest).

Schema version: pt.migration.v1

Uses CliRunner (Click 8.3+, no mix_stderr kwarg). Each test sets
PT_DB_PATH and PT_MIGRATION_DIR to tmp_path-backed locations so it never
touches the real ~/.project-tracker/migrations/ directory. The real
migration runner is used to install both 010 (handoffs, for FK-free
sibling) and 011 (migrations table).
"""

from __future__ import annotations

import json
import os
import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from pt import cli  # noqa: E402 — path setup precedes import
from db.migration_runner import (  # noqa: E402
    apply_migration,
    discover_migrations,
)


_MIGRATIONS_DIR = SCRIPTS_DIR / "db" / "migrations"


def _apply_migration(conn: sqlite3.Connection, version: int) -> None:
    migrations = discover_migrations(_MIGRATIONS_DIR)
    target = next(m for m in migrations if m.version == version)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    apply_migration(conn, target)


def _make_tracker_db(path: Path) -> None:
    """Use the real schema and migration history for the local manager."""
    from db.schema import create_database
    from db.migration_runner import apply_all
    create_database(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        apply_all(conn, _MIGRATIONS_DIR)
    finally:
        conn.close()


_COMMON_ENV = {
    "PT_SUPPRESS_MIGRATION_WARNING": "1",
    "PT_NO_BANNER": "1",
}


def _init_repo(repo_dir: Path) -> None:
    """Initialize a small git repo with one committed file so HEAD exists."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@example"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True
    )
    (repo_dir / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "seed"], check=True
    )


@pytest.fixture
def repo_env(tmp_path: Path):
    """Set up a temp git repo + DB + isolated PT_MIGRATION_DIR; cwd into the repo."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir)

    db_path = tmp_path / "tracker.db"
    _make_tracker_db(db_path)

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    env = {
        **_COMMON_ENV,
        "PT_DB_PATH": str(db_path),
        "PT_MIGRATION_DIR": str(state_dir),
    }

    prev_cwd = os.getcwd()
    os.chdir(repo_dir)
    try:
        yield {
            "tmp_path": tmp_path,
            "repo_dir": repo_dir,
            "db_path": db_path,
            "state_dir": state_dir,
            "env": env,
        }
    finally:
        os.chdir(prev_cwd)


def _invoke(env: dict, args: list[str]):
    runner = CliRunner()
    return runner.invoke(cli, args, env=env, catch_exceptions=False)


# ---------------------------------------------------------------------------
# start — happy path + validation
# ---------------------------------------------------------------------------


def test_migration_start_creates_state_file(repo_env) -> None:
    """start writes a state file with baseline HEAD + porcelain snapshot."""
    result = _invoke(repo_env["env"], ["migration", "start", "bulk-rename", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "pt.migration.v1"
    assert payload["ok"] is True
    assert payload["command"] == "migration.start"
    state_path = Path(payload["result"]["state_path"])
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["name"] == "bulk-rename"
    assert state["baseline_head"]  # HEAD captured
    assert state["baseline_porcelain"] == []


def test_migration_start_already_running_errors(repo_env) -> None:
    """Starting twice without finish errors and mentions --force."""
    first = _invoke(repo_env["env"], ["migration", "start", "twice", "--json"])
    assert first.exit_code == 0, first.output
    second = _invoke(repo_env["env"], ["migration", "start", "twice", "--json"])
    assert second.exit_code == 2
    payload = json.loads(second.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "--force" in payload["error"]["message"]


def test_migration_start_force_overwrites(repo_env) -> None:
    """--force overwrites existing state file."""
    _invoke(repo_env["env"], ["migration", "start", "twice", "--json"])
    result = _invoke(repo_env["env"], ["migration", "start", "twice", "--force", "--json"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("bad_name", ["has spaces", "has/slash", "a" * 61, ""])
def test_migration_start_invalid_name(repo_env, bad_name: str) -> None:
    """Names with spaces, slashes, or >60 chars are rejected."""
    result = _invoke(repo_env["env"], ["migration", "start", bad_name, "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"


# ---------------------------------------------------------------------------
# finish — manifest content
# ---------------------------------------------------------------------------


def test_migration_finish_manifest_carries_diff_and_writes_no_file(repo_env) -> None:
    """finish returns the manifest with the touched paths; the repo gets no MIGRATIONS.md."""
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "doc-sweep", "--json"])

    # Touch a new file and modify the seeded one.
    (repo_dir / "NEW.md").write_text("new\n", encoding="utf-8")
    (repo_dir / "README.md").write_text("seed-changed\n", encoding="utf-8")

    result = _invoke(repo_env["env"], ["migration", "finish", "doc-sweep", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["command"] == "migration.finish"
    assert payload["result"]["action"] == "manifest-only"
    assert "manifest_path" not in payload["result"]

    added_paths = [e["path"] for e in payload["result"]["added"]]
    assert "NEW.md" in added_paths
    assert "README.md" in added_paths

    manifest = payload["result"]["manifest"]
    assert manifest["name"] == "doc-sweep"
    assert manifest["action"] == "manifest-only"
    assert manifest["baseline_head"]
    manifest_paths = [e["path"] for e in manifest["added"] + manifest["modified"]]
    assert "NEW.md" in manifest_paths
    assert "README.md" in manifest_paths

    assert not (repo_dir / "MIGRATIONS.md").exists()


def test_migration_finish_prints_manifest_section(repo_env) -> None:
    """Human output prints the section in place of a `manifest: <path>` line."""
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "printed"])
    (repo_dir / "PRINTED.md").write_text("x\n", encoding="utf-8")

    result = _invoke(repo_env["env"], ["migration", "finish", "printed"])
    assert result.exit_code == 0, result.output
    assert "## printed — " in result.output
    assert "### New paths (introduced during session)" in result.output
    assert "`PRINTED.md`" in result.output
    assert "manifest:" not in result.output
    assert not (repo_dir / "MIGRATIONS.md").exists()


def test_migration_finish_leaves_existing_migrations_md_untouched(repo_env) -> None:
    """History in an existing MIGRATIONS.md is not appended to."""
    repo_dir: Path = repo_env["repo_dir"]
    (repo_dir / "MIGRATIONS.md").write_text("# history\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "MIGRATIONS.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "chore: history"], check=True
    )
    _invoke(repo_env["env"], ["migration", "start", "first", "--json"])
    (repo_dir / "A.md").write_text("a\n", encoding="utf-8")
    result = _invoke(repo_env["env"], ["migration", "finish", "first", "--json"])
    assert result.exit_code == 0, result.output
    assert (repo_dir / "MIGRATIONS.md").read_text() == "# history\n"


def test_migration_finish_missing_state_errors(repo_env) -> None:
    """finish without a state file is a clean validation error."""
    result = _invoke(repo_env["env"], ["migration", "finish", "nope", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"
    assert "no state file" in payload["error"]["message"]


def test_migration_finish_commit_revert_exclusive(repo_env) -> None:
    """--commit and --revert together is a validation error."""
    _invoke(repo_env["env"], ["migration", "start", "x", "--json"])
    result = _invoke(
        repo_env["env"], ["migration", "finish", "x", "--commit", "--revert", "--json"]
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "mutually exclusive" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# finish --revert — tracked files
# ---------------------------------------------------------------------------


def test_migration_finish_revert_restores_tracked(repo_env) -> None:
    """--revert runs `git restore` on tracked files that were modified."""
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "undo", "--json"])

    (repo_dir / "README.md").write_text("MODIFIED\n", encoding="utf-8")
    # Confirm modification is dirty before revert.
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    )
    assert "README.md" in out.stdout

    result = _invoke(repo_env["env"], ["migration", "finish", "undo", "--revert", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["action"] == "reverted"
    assert "README.md" in payload["result"]["revert_summary"]["restored"]

    # File should be back to seeded content.
    assert (repo_dir / "README.md").read_text() == "seed\n"


# ---------------------------------------------------------------------------
# finish --revert — untracked files
# ---------------------------------------------------------------------------


def test_migration_finish_revert_trashes_untracked(repo_env) -> None:
    """--revert trashes untracked files via send2trash (NOT raw rm)."""
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "untracked-sweep", "--json"])

    new_file = repo_dir / "JUNK.md"
    new_file.write_text("junk\n", encoding="utf-8")
    assert new_file.exists()

    result = _invoke(
        repo_env["env"], ["migration", "finish", "untracked-sweep", "--revert", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "JUNK.md" in payload["result"]["revert_summary"]["trashed"]
    assert not new_file.exists()


def test_revert_paths_handles_already_missing_untracked(repo_env) -> None:
    """_revert_paths logs 'already missing' for untracked entries whose file
    has been deleted before send2trash is called (race-condition path)."""
    from pt import _revert_paths

    entries = [{"path": "VANISHED.md", "classification": "untracked"}]
    restored, trashed, errors = _revert_paths(repo_env["repo_dir"], entries)
    assert restored == []
    assert trashed == []
    assert any(p == "VANISHED.md" and "already missing" in r for p, r in errors)


# ---------------------------------------------------------------------------
# baseline drift — HEAD moved during session
# ---------------------------------------------------------------------------


def test_migration_finish_baseline_drift_warning(repo_env) -> None:
    """HEAD-moved-since-start writes a head_drift_warning but still completes."""
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "drift", "--json"])

    # Make a commit after start so HEAD drifts.
    (repo_dir / "drift.md").write_text("drift\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "drift.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-q", "-m", "drift commit"], check=True
    )

    result = _invoke(repo_env["env"], ["migration", "finish", "drift", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"]["head_drift_warning"] is not None
    assert "drifted" in payload["result"]["head_drift_warning"]

    assert not (repo_dir / "MIGRATIONS.md").exists()


# ---------------------------------------------------------------------------
# list — DB-backed listing
# ---------------------------------------------------------------------------


def test_migration_list_shows_recorded_sessions(repo_env) -> None:
    """list --json returns rows for both open and closed sessions."""
    _invoke(repo_env["env"], ["migration", "start", "alpha", "--json"])
    _invoke(repo_env["env"], ["migration", "finish", "alpha", "--json"])
    _invoke(repo_env["env"], ["migration", "start", "beta", "--json"])

    result = _invoke(repo_env["env"], ["migration", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    names = [r["name"] for r in payload["result"]]
    assert "alpha" in names
    assert "beta" in names
    statuses = {r["name"]: r["status"] for r in payload["result"]}
    assert statuses["alpha"] == "finished"
    assert statuses["beta"] == "recording"


# ---------------------------------------------------------------------------
# state-file lifecycle — file is removed on finish so name is reusable
# ---------------------------------------------------------------------------


def test_state_file_removed_on_finish(repo_env) -> None:
    """After finish, the state file is gone (name can be reused)."""
    _invoke(repo_env["env"], ["migration", "start", "reuse", "--json"])
    state_path = repo_env["state_dir"] / "reuse.json"
    assert state_path.exists()
    _invoke(repo_env["env"], ["migration", "finish", "reuse", "--json"])
    assert not state_path.exists()

    # Should be startable again without --force.
    result = _invoke(repo_env["env"], ["migration", "start", "reuse", "--json"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# #6851 — the recorded repo is the CALLER's repo, not project-tracker
# ---------------------------------------------------------------------------


def test_start_records_caller_repo_not_launcher_dir(repo_env, monkeypatch) -> None:
    """The `pt` launcher does `cd "$launcher_dir"` before exec, so Path.cwd()
    inside pt.py is always project-tracker. PT_CALLER_CWD is the real answer."""
    repo_dir: Path = repo_env["repo_dir"]
    # Simulate the launcher: cwd is project-tracker, PT_CALLER_CWD is the repo.
    monkeypatch.chdir(Path(__file__).parent.parent)
    env = {**repo_env["env"], "PT_CALLER_CWD": str(repo_dir)}

    result = _invoke(env, ["migration", "start", "caller", "--json"])
    assert result.exit_code == 0, result.output

    state = json.loads((repo_env["state_dir"] / "caller.json").read_text())
    assert Path(state["repo_dir"]).resolve() == repo_dir.resolve()


def test_start_records_repo_root_when_called_from_a_subdirectory(
    repo_env, monkeypatch
) -> None:
    """`--show-toplevel` on top of PT_CALLER_CWD, which Path.cwd() never did."""
    repo_dir: Path = repo_env["repo_dir"]
    subdir = repo_dir / "src" / "deep"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(Path(__file__).parent.parent)
    env = {**repo_env["env"], "PT_CALLER_CWD": str(subdir)}

    result = _invoke(env, ["migration", "start", "subdir", "--json"])
    assert result.exit_code == 0, result.output

    state = json.loads((repo_env["state_dir"] / "subdir.json").read_text())
    assert Path(state["repo_dir"]).resolve() == repo_dir.resolve()


def test_finish_refuses_revert_when_state_repo_disagrees(repo_env, tmp_path) -> None:
    """Stale state files written before the fix carry project-tracker as their
    repo_dir. `--revert` runs `git restore` and trashes files under that path,
    so finishing one from a different tree must refuse, not guess."""
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "stale", "--json"])

    # Rewrite the state file the way a pre-#6851 run would have left it.
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    _init_repo(other_repo)
    state_path = repo_env["state_dir"] / "stale.json"
    state = json.loads(state_path.read_text())
    state["repo_dir"] = str(other_repo)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    (repo_dir / "VICTIM.md").write_text("do not trash me\n", encoding="utf-8")

    result = _invoke(
        repo_env["env"], ["migration", "finish", "stale", "--revert", "--json"]
    )
    assert result.exit_code != 0
    assert "refusing --revert" in result.output
    assert str(other_repo) in result.output
    # Nothing was touched, and the state file is still there to retry.
    assert (repo_dir / "VICTIM.md").exists()
    assert state_path.exists()


def test_finish_without_revert_still_works_on_a_mismatched_state_file(
    repo_env, tmp_path
) -> None:
    """The refusal is scoped to --revert, the destructive path. A manifest-only
    finish stays available so a stale record can be closed out."""
    _invoke(repo_env["env"], ["migration", "start", "stale2", "--json"])

    other_repo = tmp_path / "other2"
    other_repo.mkdir()
    _init_repo(other_repo)
    state_path = repo_env["state_dir"] / "stale2.json"
    state = json.loads(state_path.read_text())
    state["repo_dir"] = str(other_repo)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _invoke(repo_env["env"], ["migration", "finish", "stale2", "--json"])
    assert result.exit_code == 0, result.output


def test_finish_allows_revert_when_repos_agree(repo_env) -> None:
    """The guard must not break the normal case."""
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "agree", "--json"])
    (repo_dir / "JUNK.md").write_text("junk\n", encoding="utf-8")

    result = _invoke(
        repo_env["env"], ["migration", "finish", "agree", "--revert", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert not (repo_dir / "JUNK.md").exists()


# ---------------------------------------------------------------------------
# printed manifest — sections, warning, revert, and the DB row (#8093)
# ---------------------------------------------------------------------------


def test_printed_manifest_carries_drift_warning(repo_env) -> None:
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "drift-human"])
    (repo_dir / "drift.md").write_text("drift\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "drift.md"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "drift"], check=True)

    result = _invoke(repo_env["env"], ["migration", "finish", "drift-human"])
    assert result.exit_code == 0, result.output
    warning = [line for line in result.output.splitlines() if line.startswith("- WARNING: ")]
    assert len(warning) == 1 and "drifted" in warning[0]


def test_printed_manifest_lists_modified_paths(repo_env) -> None:
    """A path already dirty at start whose status changes lands under Modified."""
    repo_dir: Path = repo_env["repo_dir"]
    (repo_dir / "README.md").write_text("dirty before start\n", encoding="utf-8")
    _invoke(repo_env["env"], ["migration", "start", "mods"])
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)

    result = _invoke(repo_env["env"], ["migration", "finish", "mods"])
    assert result.exit_code == 0, result.output
    modified = result.output.split("### Modified paths (status changed during session)", 1)[1]
    assert "`README.md`" in modified


def test_revert_prints_its_manifest(repo_env) -> None:
    repo_dir: Path = repo_env["repo_dir"]
    _invoke(repo_env["env"], ["migration", "start", "undo-human"])
    (repo_dir / "README.md").write_text("MODIFIED\n", encoding="utf-8")

    result = _invoke(repo_env["env"], ["migration", "finish", "undo-human", "--revert"])
    assert result.exit_code == 0, result.output
    assert "## undo-human — " in result.output
    assert "- action: `reverted`" in result.output
    assert (repo_dir / "README.md").read_text() == "seed\n"
    assert not (repo_dir / "MIGRATIONS.md").exists()


def test_finished_session_row_has_no_manifest_path(repo_env) -> None:
    _invoke(repo_env["env"], ["migration", "start", "rowcheck", "--json"])
    _invoke(repo_env["env"], ["migration", "finish", "rowcheck", "--json"])

    result = _invoke(repo_env["env"], ["migration", "list", "--json"])
    row = next(r for r in json.loads(result.output)["result"] if r["name"] == "rowcheck")
    assert row["status"] == "finished"
    assert row["manifest_path"] is None
