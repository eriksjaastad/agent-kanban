"""`migration finish --revert` must never touch anything outside its repo (#6890).

`_revert_paths` built its delete target as `repo_dir / entry["path"]` and
checked only `if not target.exists()`. pathlib discards the left-hand side when
the right-hand side is absolute, so a recorded path of `/abs/path/to/other-repo`
resolved to that other repo — and the next line handed it to `send2trash`.
`""` and `"."` both resolve to the repository root itself, so either would have
trashed the whole repo the revert was running in.

That is not a theoretical hole. It is what sent a sibling project to the Trash
while its developers were working, and it left no shell history and no hook log
because the deletion happened inside a Python call in another repo's process.

These tests build a real sibling directory next to a real repo and assert it
survives. Anything that can delete has to prove it cannot reach outside.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import pt as pt_cli  # noqa: E402
from commands.migration import PathEscapesRepoError, _contained_path, _revert_paths  # noqa: E402
from commands import handoff as handoff_commands  # noqa: E402


@pytest.fixture
def repo_and_sibling(tmp_path: Path) -> tuple[Path, Path]:
    """A repo, and a sibling directory that must survive every revert."""
    repo = tmp_path / "the-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "note.md").write_text("inside the repo\n")

    sibling = tmp_path / "innocent-bystander"
    sibling.mkdir()
    (sibling / "important.md").write_text("someone is working in here\n")
    return repo, sibling


# --- _contained_path, the guard itself -------------------------------------


def test_relative_path_inside_repo_is_allowed(repo_and_sibling) -> None:
    repo, _ = repo_and_sibling
    assert _contained_path(repo, "docs/note.md") == (repo / "docs/note.md").resolve()


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_path_is_refused(repo_and_sibling, bad: str) -> None:
    repo, _ = repo_and_sibling
    with pytest.raises(PathEscapesRepoError):
        _contained_path(repo, bad)


@pytest.mark.parametrize("bad", [".", "./"])
def test_dot_resolving_to_repo_root_is_refused(repo_and_sibling, bad: str) -> None:
    """`repo_dir / "."` is the repo root — trashing it deletes everything."""
    repo, _ = repo_and_sibling
    with pytest.raises(PathEscapesRepoError):
        _contained_path(repo, bad)


def test_absolute_path_is_refused(repo_and_sibling) -> None:
    """The incident path: an absolute RHS discards repo_dir entirely."""
    repo, sibling = repo_and_sibling
    with pytest.raises(PathEscapesRepoError):
        _contained_path(repo, str(sibling))


def test_parent_traversal_is_refused(repo_and_sibling) -> None:
    repo, _ = repo_and_sibling
    with pytest.raises(PathEscapesRepoError):
        _contained_path(repo, "../innocent-bystander")


def test_symlink_escape_is_refused(repo_and_sibling) -> None:
    """A symlink inside the repo must not become a way out of it."""
    repo, sibling = repo_and_sibling
    (repo / "escape-hatch").symlink_to(sibling)
    with pytest.raises(PathEscapesRepoError):
        _contained_path(repo, "escape-hatch/important.md")


# --- _revert_paths, the destructive caller ---------------------------------


def test_revert_refuses_absolute_path_and_sibling_survives(repo_and_sibling) -> None:
    """The exact shape of the incident, end to end."""
    repo, sibling = repo_and_sibling
    entries = [{"path": str(sibling), "classification": "untracked"}]

    with patch("send2trash.send2trash") as trash_mock:
        restored, trashed, errors = _revert_paths(repo, entries)

    trash_mock.assert_not_called()
    assert sibling.exists(), "a revert reached outside its own repository"
    assert (sibling / "important.md").read_text() == "someone is working in here\n"
    assert trashed == []
    assert len(errors) == 1 and "refused" in errors[0][1]


def test_revert_refuses_repo_root_itself(repo_and_sibling) -> None:
    repo, _ = repo_and_sibling
    entries = [{"path": "", "classification": "untracked"}]

    with patch("send2trash.send2trash") as trash_mock:
        _, trashed, errors = _revert_paths(repo, entries)

    trash_mock.assert_not_called()
    assert repo.exists() and (repo / "docs" / "note.md").exists()
    assert trashed == []
    assert len(errors) == 1


def test_revert_still_trashes_legitimate_untracked_files(repo_and_sibling) -> None:
    """The guard must not break the feature it protects."""
    repo, _ = repo_and_sibling
    junk = repo / "scratch.tmp"
    junk.write_text("throwaway\n")
    entries = [{"path": "scratch.tmp", "classification": "untracked"}]

    with patch("send2trash.send2trash") as trash_mock:
        _, trashed, errors = _revert_paths(repo, entries)

    trash_mock.assert_called_once_with(str(junk.resolve()))
    assert trashed == ["scratch.tmp"]
    assert errors == []


@pytest.mark.parametrize(
    "bad_path",
    [lambda sibling: str(sibling), lambda sibling: "foo\x00bar"],
    ids=["path_outside_repo", "nul_byte"],
)
def test_one_bad_path_does_not_block_the_good_ones(repo_and_sibling, bad_path) -> None:
    repo, sibling = repo_and_sibling
    junk = repo / "scratch.tmp"
    junk.write_text("throwaway\n")
    entries = [
        {"path": bad_path(sibling), "classification": "untracked"},
        {"path": "scratch.tmp", "classification": "untracked"},
    ]

    with patch("send2trash.send2trash") as trash_mock:
        _, trashed, errors = _revert_paths(repo, entries)

    assert sibling.exists()
    assert trashed == ["scratch.tmp"], "a bad entry aborted the whole revert"
    assert trash_mock.call_count == 1
    assert len(errors) == 1


def test_tracked_paths_are_screened_too(repo_and_sibling) -> None:
    """`git restore` would likely refuse an outside path, but don't rely on it."""
    repo, sibling = repo_and_sibling
    entries = [{"path": str(sibling / "important.md"), "classification": "dirty"}]

    restored, _, errors = _revert_paths(repo, entries)

    assert restored == []
    assert len(errors) == 1 and "refused" in errors[0][1]
    assert (sibling / "important.md").exists()


# --- the entry point that records these paths ------------------------------


@pytest.mark.parametrize(
    "bad_path",
    ["/etc", "", "   ", ".", "..", "../sibling", "docs/../../sibling"],
)
def test_handoff_files_rejects_unsafe_paths(bad_path: str) -> None:
    """Bad paths must never get written down in the first place."""
    with pytest.raises(handoff_commands.PtJsonError):
        handoff_commands._validate_files_array(
            [{"path": bad_path, "classification": "untracked"}]
        )


def test_handoff_files_still_accepts_normal_paths() -> None:
    handoff_commands._validate_files_array(
        [
            {"path": "docs/note.md", "classification": "untracked"},
            {"path": "scripts/thing.py", "classification": "dirty"},
        ]
    )


# --- NUL bytes must refuse cleanly, not abort the run ----------------------


def test_nul_byte_path_is_refused_cleanly(repo_and_sibling) -> None:
    """`.resolve()` raises a bare ValueError on a NUL byte.

    Uncaught, that would propagate out of the untracked loop and abort the
    whole revert instead of skipping the one bad entry — breaking the
    "one bad path doesn't block the others" guarantee.
    """
    repo, _ = repo_and_sibling
    with pytest.raises(PathEscapesRepoError):
        _contained_path(repo, "foo\x00bar")


# --- the audit trail -------------------------------------------------------


def test_deletion_is_logged_before_it_happens(repo_and_sibling, tmp_path, monkeypatch) -> None:
    """Every in-process delete must leave a record.

    The bash-validator hook cannot see a send2trash() call inside a Python
    process — which is why an earlier trashing left no trail anywhere.
    """
    import json as _json

    repo, _ = repo_and_sibling
    junk = repo / "scratch.tmp"
    junk.write_text("throwaway\n")
    log = tmp_path / "pt-destructive.log"
    monkeypatch.setenv("PT_DESTRUCTIVE_LOG_PATH", str(log))

    with patch("send2trash.send2trash"):
        _revert_paths(repo, [{"path": "scratch.tmp", "classification": "untracked"}])
    assert log.exists(), "an in-process deletion left no audit record"
    record = _json.loads(log.read_text().strip().splitlines()[-1])
    assert record["operation"] == "send2trash"
    assert record["target"] == str(junk.resolve())
    assert record["repo_dir"] == str(repo)
    assert record["pid"] > 0


def test_refused_paths_are_not_logged_as_deletions(repo_and_sibling, tmp_path, monkeypatch) -> None:
    """The log records what was deleted, not what was attempted and refused."""
    repo, sibling = repo_and_sibling
    log = tmp_path / "pt-destructive.log"
    monkeypatch.setenv("PT_DESTRUCTIVE_LOG_PATH", str(log))

    with patch("send2trash.send2trash"):
        _revert_paths(repo, [{"path": str(sibling), "classification": "untracked"}])
    assert not log.exists() or log.read_text().strip() == ""
