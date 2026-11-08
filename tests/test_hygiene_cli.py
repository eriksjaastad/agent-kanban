"""Tests for pt hygiene --json portfolio hygiene state command."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from pt import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Initialise a bare-minimum git repo with one tracked file and a commit."""
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True, capture_output=True,
    )
    # Tracked file + initial commit so the repo is non-empty
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path), check=True, capture_output=True,
    )


def _invoke(portfolio_root: Path, args: list[str]):
    """Run a pt CLI invocation with PROJECTS_ROOT overridden to portfolio_root."""
    runner = CliRunner()
    return runner.invoke(
        cli,
        args,
        env={
            "PROJECTS_ROOT": str(portfolio_root),
            "PT_SUPPRESS_MIGRATION_WARNING": "1",
            "PT_NO_BANNER": "1",
            # Keep DB operations from touching the real DB
            "PT_DB_PATH": str(portfolio_root / "tracker.db"),
        },
        catch_exceptions=False,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_clean_portfolio_exit_0_and_schema(tmp_path: Path) -> None:
    """A clean single-repo portfolio exits 0 with stable schema keys."""
    repo = tmp_path / "my-project"
    repo.mkdir()
    _init_git_repo(repo)

    result = _invoke(tmp_path, ["hygiene", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    # Schema stability
    assert payload["schema_version"] == "pt.hygiene.v2"
    assert payload["ok"] is True
    assert payload["command"] == "hygiene"
    assert "scanned_at" in payload
    assert "projects_root" in payload
    assert payload["summary"]["total_repos"] == 1
    assert payload["summary"]["clean_repos"] == 1
    assert payload["summary"]["repos_with_findings"] == 0

    # Result entry shape
    repo_result = payload["results"][0]
    assert repo_result["project"] == "my-project"
    assert repo_result["clean"] is True
    findings = repo_result["findings"]
    for key in (
        "dirty_tree",
        "local_only_branches",
        "branches_ahead_of_remote",
        "stashes",
        "stale_progress_md",
    ):
        assert key in findings, f"missing finding key: {key}"
    assert findings["dirty_tree"]["present"] is False
    assert findings["dirty_tree"]["files"] == []
    assert findings["stashes"]["present"] is False
    assert findings["stashes"]["count"] == 0


def test_dirty_tree_finding_excludes_progress_md(tmp_path: Path) -> None:
    """A dirty tree is detected but PROGRESS.md modifications are excluded."""
    repo = tmp_path / "dirty-project"
    repo.mkdir()
    _init_git_repo(repo)

    # Modify tracked file (should be detected)
    (repo / "README.md").write_text("changed\n")

    # Add PROGRESS.md as a new untracked file (should NOT count)
    (repo / "PROGRESS.md").write_text("some progress\n")

    result = _invoke(tmp_path, ["hygiene", "--json"])

    assert result.exit_code == 6, result.output  # EXIT_HYGIENE_FINDING
    payload = json.loads(result.output)
    assert payload["ok"] is False

    repo_result = payload["results"][0]
    assert repo_result["clean"] is False
    dirty = repo_result["findings"]["dirty_tree"]
    assert dirty["present"] is True
    assert "PROGRESS.md" not in dirty["files"]
    assert any("README.md" in f for f in dirty["files"])


def test_progress_md_alone_does_not_trip_dirty_tree(tmp_path: Path) -> None:
    """PROGRESS.md-only dirtiness is NOT reported in dirty_tree."""
    repo = tmp_path / "progress-only-project"
    repo.mkdir()
    _init_git_repo(repo)

    # Only PROGRESS.md is dirty (this is the documented always-dirty file)
    (repo / "PROGRESS.md").write_text("session notes\n")

    result = _invoke(tmp_path, ["hygiene", "--json"])

    payload = json.loads(result.output)
    repo_result = payload["results"][0]
    dirty = repo_result["findings"]["dirty_tree"]
    assert dirty["present"] is False
    assert "PROGRESS.md" not in dirty["files"]


def test_local_only_branch_finding(tmp_path: Path) -> None:
    """A local branch with no upstream is reported."""
    repo = tmp_path / "branchy-project"
    repo.mkdir()
    _init_git_repo(repo)

    # Create a local branch with no remote
    subprocess.run(
        ["git", "checkout", "-b", "feat/orphan"],
        cwd=str(repo), check=True, capture_output=True,
    )
    # Switch back so HEAD is on main
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=str(repo), check=True, capture_output=True,
    )

    result = _invoke(tmp_path, ["hygiene", "--json"])

    payload = json.loads(result.output)
    repo_result = payload["results"][0]
    local_only = repo_result["findings"]["local_only_branches"]
    assert local_only["present"] is True
    assert "feat/orphan" in local_only["branches"]
    # main itself should NOT appear
    assert "main" not in local_only["branches"]


def test_stash_finding(tmp_path: Path) -> None:
    """A stash is reported."""
    repo = tmp_path / "stashy-project"
    repo.mkdir()
    _init_git_repo(repo)

    # Create a stash
    (repo / "README.md").write_text("stashed change\n")
    subprocess.run(
        ["git", "stash", "push", "-m", "wip"],
        cwd=str(repo), check=True, capture_output=True,
    )

    result = _invoke(tmp_path, ["hygiene", "--json"])

    payload = json.loads(result.output)
    repo_result = payload["results"][0]
    stashes = repo_result["findings"]["stashes"]
    assert stashes["present"] is True
    assert stashes["count"] >= 1


def test_project_filter_narrows_to_one_repo(tmp_path: Path) -> None:
    """--project NAME scans only that repo."""
    for name in ("alpha", "beta", "gamma"):
        repo = tmp_path / name
        repo.mkdir()
        _init_git_repo(repo)

    result = _invoke(tmp_path, ["hygiene", "--json", "--project", "beta"])

    payload = json.loads(result.output)
    assert payload["summary"]["total_repos"] == 1
    assert payload["results"][0]["project"] == "beta"


def test_project_filter_invalid_name_exits_2(tmp_path: Path) -> None:
    """--project with a nonexistent name exits 2 (validation error)."""
    repo = tmp_path / "real-repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _invoke(tmp_path, ["hygiene", "--json", "--project", "does-not-exist"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "validation"


def test_quiet_flag_hides_clean_repos(tmp_path: Path) -> None:
    """--quiet suppresses clean repos in human output (non-JSON mode)."""
    repo_clean = tmp_path / "clean-repo"
    repo_clean.mkdir()
    _init_git_repo(repo_clean)

    repo_dirty = tmp_path / "dirty-repo"
    repo_dirty.mkdir()
    _init_git_repo(repo_dirty)
    (repo_dirty / "README.md").write_text("dirty\n")

    result = _invoke(tmp_path, ["hygiene", "--quiet"])

    # Should see the dirty repo but not the clean one in output
    assert "dirty-repo" in result.output
    assert "clean-repo" not in result.output


def test_json_schema_all_keys_present(tmp_path: Path) -> None:
    """Every documented key in the pt.hygiene.v2 schema must be present."""
    repo = tmp_path / "schema-test"
    repo.mkdir()
    _init_git_repo(repo)

    result = _invoke(tmp_path, ["hygiene", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)

    # Top-level envelope
    required_top = {"schema_version", "ok", "command", "scanned_at", "projects_root", "summary", "results"}
    assert required_top <= set(payload.keys())

    # Summary
    required_summary = {"total_repos", "clean_repos", "repos_with_findings"}
    assert required_summary <= set(payload["summary"].keys())

    # Per-repo entry
    entry = payload["results"][0]
    required_entry = {"project", "path", "clean", "findings"}
    assert required_entry <= set(entry.keys())

    # Findings keys
    required_findings = {
        "dirty_tree",
        "local_only_branches",
        "branches_ahead_of_remote",
        "stashes",
        "stale_progress_md",
    }
    assert required_findings <= set(entry["findings"].keys())

    # Nested shapes
    assert "present" in entry["findings"]["dirty_tree"]
    assert "files" in entry["findings"]["dirty_tree"]
    assert "present" in entry["findings"]["stashes"]
    assert "count" in entry["findings"]["stashes"]
    assert "present" in entry["findings"]["local_only_branches"]
    assert "branches" in entry["findings"]["local_only_branches"]
    assert "present" in entry["findings"]["branches_ahead_of_remote"]
    assert "branches" in entry["findings"]["branches_ahead_of_remote"]
    assert "present" in entry["findings"]["stale_progress_md"]
    assert "age_days" in entry["findings"]["stale_progress_md"]


# ---------------------------------------------------------------------------
# Per-repo isolation (HIGH #1)
# ---------------------------------------------------------------------------


def test_broken_repo_isolated_in_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure in one repo's scan does not abort other repos."""
    import pt as pt_mod

    # Two healthy repos plus one "broken" one
    for name in ("alpha", "broken", "gamma"):
        repo = tmp_path / name
        repo.mkdir()
        _init_git_repo(repo)

    real_scan = pt_mod._hygiene_scan_repo

    def fake_scan(repo_dir: Path, json_mode: bool):
        if repo_dir.name == "broken":
            raise PermissionError("simulated permission error on broken repo")
        return real_scan(repo_dir, json_mode)

    monkeypatch.setattr(pt_mod, "_hygiene_scan_repo", fake_scan)

    result = _invoke(tmp_path, ["hygiene", "--json"])

    payload = json.loads(result.output)
    assert payload["summary"]["total_repos"] == 3

    by_name = {r["project"]: r for r in payload["results"]}
    assert by_name["broken"]["clean"] is False
    assert by_name["broken"]["error"]["class"] == "PermissionError"
    assert "simulated permission error" in by_name["broken"]["error"]["message"]
    # Other repos must still scan cleanly despite the broken neighbor
    assert by_name["alpha"]["clean"] is True
    assert by_name["gamma"]["clean"] is True
    # Findings exit code fires because at least one repo is non-clean
    assert result.exit_code == 6


def test_error_entry_omits_findings_per_schema_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pt.hygiene.v2 contract: error entries omit `findings` entirely;
    successful entries include all six finding keys.

    Naive consumers MUST branch on `error` before iterating `findings`.
    An empty findings dict would read as "checked and found nothing" —
    wrong semantics. Absence reads correctly as "couldn't check."
    """
    import pt as pt_mod

    for name in ("clean-one", "broken-one"):
        repo = tmp_path / name
        repo.mkdir()
        _init_git_repo(repo)

    real_scan = pt_mod._hygiene_scan_repo

    def fake_scan(repo_dir: Path, json_mode: bool):
        if repo_dir.name == "broken-one":
            raise PermissionError("simulated scan failure")
        return real_scan(repo_dir, json_mode)

    monkeypatch.setattr(pt_mod, "_hygiene_scan_repo", fake_scan)

    result = _invoke(tmp_path, ["hygiene", "--json"])
    payload = json.loads(result.output)
    by_name = {r["project"]: r for r in payload["results"]}

    # Error entry: error present, findings ABSENT
    broken = by_name["broken-one"]
    assert "error" in broken
    assert broken["error"]["class"] == "PermissionError"
    assert "findings" not in broken, (
        "Schema contract: error entries must omit `findings` so naive "
        "consumers iterating findings KeyError loudly rather than reading "
        "an empty dict as 'checked and found nothing'."
    )

    # Clean entry: findings present with all six keys
    clean = by_name["clean-one"]
    assert "error" not in clean
    assert "findings" in clean
    expected_keys = {
        "dirty_tree",
        "local_only_branches",
        "branches_ahead_of_remote",
        "stashes",
        "stale_progress_md",
    }
    assert expected_keys <= set(clean["findings"].keys())


# ---------------------------------------------------------------------------
# PROGRESS.md exclusion in stale check (HIGH #2)
# ---------------------------------------------------------------------------


def _age_progress_md(repo: Path, days: float) -> None:
    """Set PROGRESS.md's mtime/atime to now - days."""
    import os as _os
    import time as _time
    target = _time.time() - (days * 86400)
    _os.utime(repo / "PROGRESS.md", (target, target))


def test_stale_progress_md_only_progress_dirty_does_not_fire(tmp_path: Path) -> None:
    """Old PROGRESS.md + ONLY PROGRESS.md is dirty -> stale finding does NOT fire."""
    repo = tmp_path / "stale-progress-only"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "PROGRESS.md").write_text("ancient progress notes\n")
    _age_progress_md(repo, days=14)

    result = _invoke(tmp_path, ["hygiene", "--json"])

    payload = json.loads(result.output)
    repo_result = payload["results"][0]
    stale = repo_result["findings"]["stale_progress_md"]
    assert stale["present"] is False, (
        "PROGRESS.md being the ONLY dirty file should NOT trigger the stale "
        "finding -- the convention is that PROGRESS.md is always dirty."
    )


def test_stale_progress_md_with_other_dirty_file_fires(tmp_path: Path) -> None:
    """Old PROGRESS.md + non-PROGRESS dirty file -> stale finding fires."""
    repo = tmp_path / "stale-progress-with-work"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "PROGRESS.md").write_text("ancient progress notes\n")
    _age_progress_md(repo, days=14)

    # Real uncommitted work besides PROGRESS.md
    (repo / "README.md").write_text("real changes\n")

    result = _invoke(tmp_path, ["hygiene", "--json"])

    payload = json.loads(result.output)
    repo_result = payload["results"][0]
    stale = repo_result["findings"]["stale_progress_md"]
    assert stale["present"] is True
    assert stale["age_days"] >= 7


# ---------------------------------------------------------------------------
# branches_ahead_of_remote (MEDIUM #3a)
# ---------------------------------------------------------------------------


def test_branch_ahead_of_remote_finding(tmp_path: Path) -> None:
    """A local branch with commits ahead of its configured upstream is reported."""
    # Build a bare "remote" and a working clone
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )

    work = tmp_path / "ahead-repo"
    subprocess.run(
        ["git", "clone", str(remote), str(work)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example"],
        cwd=str(work), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(work), check=True, capture_output=True,
    )

    # Initial commit pushed
    (work / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=str(work), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(work), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(work), check=True, capture_output=True,
    )

    # Local commit not yet pushed -> ahead by 1
    (work / "README.md").write_text("local change\n")
    subprocess.run(["git", "add", "."], cwd=str(work), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "local"],
        cwd=str(work), check=True, capture_output=True,
    )

    result = _invoke(tmp_path, ["hygiene", "--json", "--project", "ahead-repo"])

    payload = json.loads(result.output)
    repo_result = payload["results"][0]
    ahead = repo_result["findings"]["branches_ahead_of_remote"]
    assert ahead["present"] is True
    main_entry = next((b for b in ahead["branches"] if b["name"] == "main"), None)
    assert main_entry is not None, ahead
    assert main_entry["ahead"] >= 1


def test_git_status_failure_is_a_scan_error_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6900: a failed `git status` must not read as a clean working tree."""
    import pt as pt_mod

    _init_git_repo(tmp_path / "repo")

    def failing_git(repo_dir, args, timeout=10):
        if args[:1] == ["status"]:
            raise subprocess.TimeoutExpired(cmd="git status", timeout=timeout)
        return "", 0

    monkeypatch.setattr(pt_mod, "_run_git", failing_git)
    result = _invoke(tmp_path, ["hygiene", "--json"])
    payload = json.loads(result.output)
    entry = payload["results"][0]
    assert entry["clean"] is False
    assert entry["error"]["class"] == "TimeoutExpired"
    assert "findings" not in entry


@pytest.mark.parametrize("command", ["status", "branch", "for-each-ref", "stash"])
def test_a_nonzero_git_exit_is_a_scan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """#6900: a git listing that fails with empty output is not a clean repo."""
    import pt as pt_mod

    _init_git_repo(tmp_path / "repo")

    def failing_git(repo_dir, args, timeout=10):
        if args[:1] == [command]:
            return "", 128
        return {"for-each-ref": "", "branch": ""}.get(args[0], ""), 0

    monkeypatch.setattr(pt_mod, "_run_git", failing_git)
    result = _invoke(tmp_path, ["hygiene", "--json"])
    entry = json.loads(result.output)["results"][0]
    assert entry["clean"] is False
    assert entry["error"]["class"] == "RuntimeError"
