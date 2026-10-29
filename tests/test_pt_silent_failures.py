"""pt.py failures that used to read as valid results (#6900).

Each test pins one handler that previously turned an operation failure into
a value indistinguishable from a real answer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pt  # noqa: E402


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(cmd="git worktree list", timeout=10),
        subprocess.CalledProcessError(128, "git worktree list", stderr="fatal: boom"),
    ],
)
def test_worktree_branch_failure_raises_instead_of_orphaned(monkeypatch, tmp_path, error):
    """`pt worktrees clean` removes worktrees whose branch is None, so a
    failed git lookup must not return None."""

    def failing_run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(pt.subprocess, "run", failing_run)
    with pytest.raises(click.ClickException):
        pt._worktree_branch(tmp_path, tmp_path / "wt")


def test_porcelain_baseline_failure_raises_instead_of_empty(monkeypatch, tmp_path):
    """An empty baseline at `migration start` makes every pre-existing dirty
    file look new at finish, where `--revert` would restore or trash it."""

    def failing_git(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git status", timeout=10)

    monkeypatch.setattr(pt, "_run_git", failing_git)
    with pytest.raises(pt.PtJsonError) as excinfo:
        pt._git_porcelain_baseline(tmp_path)
    assert excinfo.value.exit_code == pt.EXIT_QUERY_FAILURE


def test_migration_start_reports_git_failure_and_writes_no_state(monkeypatch, tmp_path):
    from click.testing import CliRunner

    def failing_git(repo_dir, args, timeout=10):
        if args[:1] == ["status"]:
            raise subprocess.TimeoutExpired(cmd="git status", timeout=timeout)
        return "", 128

    monkeypatch.setattr(pt, "_run_git", failing_git)
    monkeypatch.setenv("PT_MIGRATION_DIR", str(tmp_path / "migrations"))
    monkeypatch.setenv("PT_CALLER_CWD", str(tmp_path))
    result = CliRunner().invoke(pt.cli, ["migration", "start", "sweep-test", "--json"])
    assert result.exit_code == pt.EXIT_QUERY_FAILURE, result.output
    assert '"query_failure"' in result.output
    assert not (tmp_path / "migrations" / "sweep-test.json").exists()


def test_known_chat_addresses_warns_when_unverifiable(monkeypatch, capsys):
    """The "cannot verify" None skips recipient verification; the
    operator must be told verification did not happen."""

    def broken_db():
        raise RuntimeError("db down")

    monkeypatch.setattr(pt, "DatabaseManager", broken_db)
    assert pt._known_chat_addresses() is None
    assert "could not verify the recipient address" in capsys.readouterr().err


def test_unresolvable_caller_dir_is_not_replaced_by_sender(monkeypatch, tmp_path):
    """A calling directory that cannot be resolved must surface, not
    silently fall back to the configured sender address."""
    def broken_resolve(self, strict=False):
        raise OSError("resolve failed")

    monkeypatch.setenv("PT_CALLER_CWD", str(tmp_path))
    monkeypatch.setattr(Path, "resolve", broken_resolve)
    with pytest.raises(OSError):
        pt._resolve_self_address({"sender": "claude-architect"})
