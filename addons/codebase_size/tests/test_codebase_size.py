"""Codebase size scanner, append-only runs, score and `pt size` (#8083)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from addons.codebase_size import scanner as cs
from pt import cli



# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, date: str | None = None, data: bytes | None = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, env=env, input=data,
    )
    return proc.stdout.decode().strip()


def make_repo(root: Path, name: str, files: dict[str, str],
              date: str = "2026-01-01T00:00:00+0000") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example")
    _git(repo, "config", "commit.gpgsign", "false")
    commit(repo, files, date)
    return repo


def commit(repo: Path, files: dict[str, str], date: str) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c", date=date)


FILES = {
    "app.py": "a\nb\nc\n",                        # code 3
    "tool.sh": "x\ny",                            # code 2 (unterminated last line)
    "src/mod.ts": "1\n2\n3\n4\n",                 # code 4
    "tests/test_app.py": "t1\nt2\n",              # test 2
    "src/widget.test.ts": "w\n",                  # test 1
    "pkg/helper_test.go": "g\ng\ng\n",            # test 3
    "node_modules/lib/index.js": "n\n" * 50,      # skipped
    "scripts/db/migrations/001_x.py": "m\n" * 9,  # skipped
    "web/app.min.js": "z\n" * 7,                  # skipped
    "package-lock.json": "{}\n",                  # not code anyway
    "data/fixture.py": "d\n" * 11,                # skipped (data/)
    "README.md": "r1\nr2\nr3\n",                  # doc 3 lines
    "docs/guide.md": "g1\ng2",                    # doc 2 lines
    "notes.txt": "ignored\n",                     # not code
}

EXPECTED = (9, 6, 2, 5)  # code, tests, doc_files, doc_lines


def survey(repo: Path) -> tuple[int, int, int]:
    """Independent re-implementation of the 2026-10-08 survey loop."""
    code_re = re.compile(r"\.(py|js|ts|tsx|jsx|sh|swift|go|rs|mjs|cjs)$")
    skip_re = re.compile(
        r"(^|/)(node_modules|vendor|dist|build|\.venv|venv|migrations|fixtures|data)/"
        r"|\.min\.js$|package-lock|\.lock$"
    )
    test_re = re.compile(
        r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.|\.test\.|\.spec\."
    )
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split("\n")
    code = test = docs = 0
    for f in out:
        if not f or skip_re.search(f):
            continue
        if f.endswith(".md"):
            docs += 1
            continue
        if not code_re.search(f):
            continue
        n = sum(1 for _ in open(repo / f, errors="ignore"))
        if test_re.search(f):
            test += n
        else:
            code += n
    return code, test, docs


@pytest.fixture
def mgr():
    from addons.codebase_size.db import CodebaseSizeDB

    return CodebaseSizeDB()


@pytest.fixture
def conn():
    db = sqlite3.connect(os.environ["PT_DB_PATH"])
    yield db
    db.close()


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM codebase_size_snapshots").fetchone()[0]


def _runs(conn) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM codebase_size_snapshots"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def test_classification_and_line_counts(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj", FILES)
    size = cs.scan_repo(repo)
    assert size.error is None
    assert (size.code, size.tests, size.doc_files, size.doc_lines) == EXPECTED
    assert (size.code, size.tests, size.doc_files) == survey(repo)
    assert size.last_commit == "2026-01-01"


def test_listed_but_absent_file_is_skipped_but_git_failure_is_an_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj", FILES)
    # A path git lists that is not on disk: index entry for a blob with no file.
    blob = _git(repo, "hash-object", "-w", "--stdin", data=b"1\n2\n3\n4\n5\n")
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},ghost.py")
    assert "ghost.py" in _git(repo, "ls-files")
    size = cs.scan_repo(repo)
    assert size.error is None
    assert size.code == EXPECTED[0]  # ghost.py contributes nothing

    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)
    result = cs.scan_repo(broken)
    assert result.error and "git" in result.error
    assert result.code == 0


def test_listed_but_absent_md_counts_as_a_doc_file_like_the_survey(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj", FILES)
    blob = _git(repo, "hash-object", "-w", "--stdin", data=b"l1\nl2\n")
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},ghost.md")
    size = cs.scan_repo(repo)
    assert size.error is None
    assert size.doc_files == EXPECTED[2] + 1
    assert size.doc_lines == EXPECTED[3]  # nothing on disk to count
    assert (size.code, size.tests, size.doc_files) == survey(repo)


def test_empty_files_count_as_zero_lines(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "proj", {**FILES, "empty.py": "", "empty.md": ""})
    size = cs.scan_repo(repo)
    assert (size.code, size.tests, size.doc_files, size.doc_lines) == (
        EXPECTED[0], EXPECTED[1], EXPECTED[2] + 1, EXPECTED[3])


def test_repo_with_no_commits_is_measured_not_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("1\n2\n")
    _git(repo, "add", "a.py")
    size = cs.scan_repo(repo)
    assert size.error is None
    assert (size.code, size.last_commit, size.commits_90d) == (2, None, 0)


def test_scan_measures_the_checked_out_tree(tmp_path: Path) -> None:
    # Best effort by design: a repo on a feature branch is measured as that branch.
    repo = make_repo(tmp_path, "proj", FILES)
    _git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, {"extra.py": "1\n2\n"}, "2026-01-02T00:00:00+0000")
    assert cs.scan_repo(repo).code == EXPECTED[0] + 2


def test_portfolio_skips_worktree_dirs_and_non_repos(tmp_path: Path) -> None:
    make_repo(tmp_path, "big", {"a.py": "x\n" * 10})
    make_repo(tmp_path, "small", {"a.py": "x\n"})
    make_repo(tmp_path, "big-wt-feature", {"a.py": "x\n" * 99})
    (tmp_path / "plain").mkdir()
    rows = cs.scan_portfolio(tmp_path)
    assert [(r.project, r.code) for r in rows] == [("big", 10), ("small", 1)]


def test_missing_projects_root_raises(tmp_path: Path) -> None:
    with pytest.raises(cs.ProjectsRootMissing):
        cs.scan_portfolio(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def _size(code=100, tests=50, doc_files=2, doc_lines=40) -> cs.RepoSize:
    return cs.RepoSize("p", code, tests, doc_files, doc_lines)


def test_score_shrink_is_positive() -> None:
    score = cs.optimization_score(_size(), _size(code=70))
    assert score == pytest.approx(30 / 140 * 100)
    assert score > 0


def test_score_growth_is_negative() -> None:
    assert cs.optimization_score(_size(), _size(code=200)) < 0


def test_score_moving_docs_into_code_is_neutral() -> None:
    assert cs.optimization_score(_size(), _size(code=130, doc_lines=10)) == 0.0


def test_score_deleting_redundant_docs_is_positive() -> None:
    assert cs.optimization_score(_size(), _size(doc_lines=10)) > 0


def test_score_ignores_test_lines() -> None:
    assert cs.optimization_score(_size(), _size(tests=0)) == 0.0
    assert cs.optimization_score(_size(), _size(tests=5000)) == 0.0


def test_score_none_without_usable_baseline() -> None:
    assert cs.optimization_score(None, _size()) is None
    assert cs.optimization_score(_size(code=0, doc_lines=0), _size()) is None


def test_score_text_never_shows_negative_zero():
    from addons.codebase_size.cli import _size_score_text

    assert _size_score_text(None) == "-"
    assert _size_score_text(-0.006) == "+0.0"
    assert _size_score_text(-0.06) == "-0.1"
    assert _size_score_text(12.34) == "+12.3"


# ---------------------------------------------------------------------------
# storage: append-only runs; the earliest run is the baseline
# ---------------------------------------------------------------------------


def test_no_runs_means_no_baseline(mgr) -> None:
    assert mgr.codebase_baseline() == (None, {})
    assert mgr.codebase_latest_scan() == (None, {})


def test_runs_append_baseline_is_earliest_and_latest_is_newest(mgr, conn) -> None:
    first = mgr.save_codebase_scan([cs.RepoSize("a", code=1), cs.RepoSize("b", code=2)], "2026-10-08")
    second = mgr.save_codebase_scan([cs.RepoSize("a", code=9)], "2026-10-08")
    third = mgr.save_codebase_scan([cs.RepoSize("a", code=7), cs.RepoSize("c", code=3)], "2026-10-09")
    assert len({first, second, third}) == 3
    # Nothing was replaced or deleted.
    assert (_runs(conn), _count(conn)) == (3, 5)
    date, base = mgr.codebase_baseline()
    assert date == "2026-10-08"
    assert {p: r.code for p, r in base.items()} == {"a": 1, "b": 2}
    # The newest run is read whole: b does not leak in from an older run.
    date, latest = mgr.codebase_latest_scan()
    assert date == "2026-10-09"
    assert {p: r.code for p, r in latest.items()} == {"a": 7, "c": 3}


def test_save_refuses_errored_empty_or_duplicate_sets_before_any_write(mgr, conn) -> None:
    mgr.save_codebase_scan([cs.RepoSize("a", code=5)], "2026-10-08")
    with pytest.raises(ValueError, match="errored"):
        mgr.save_codebase_scan([cs.RepoSize("a", error="boom")], "2026-10-08")
    with pytest.raises(ValueError, match="empty"):
        mgr.save_codebase_scan([], "2026-10-08")
    with pytest.raises(ValueError, match="duplicate"):
        mgr.save_codebase_scan([cs.RepoSize("b"), cs.RepoSize("b")], "2026-10-08")
    assert (_runs(conn), _count(conn)) == (1, 1)


def test_table_refuses_update_and_delete(mgr, conn) -> None:
    mgr.save_codebase_scan([cs.RepoSize("a", code=5)], "2026-10-08")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE codebase_size_snapshots SET code_lines = 0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM codebase_size_snapshots")
    conn.rollback()
    assert mgr.codebase_baseline()[1]["a"].code == 5


# ---------------------------------------------------------------------------
# pt size CLI
# ---------------------------------------------------------------------------

_ENV = {"PT_NO_BANNER": "1", "PT_SUPPRESS_MIGRATION_WARNING": "1"}


def _pt(monkeypatch, root: Path, args: list[str]):
    monkeypatch.setenv("PROJECTS_ROOT", str(root))
    return CliRunner().invoke(cli, args, env=dict(_ENV))


def _json(output: str) -> dict:
    return json.loads(output[: output.rindex("}") + 1])


def test_pt_size_first_snapshot_is_the_baseline_then_scores(tmp_path: Path, monkeypatch, conn) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    repo = make_repo(root, "proj", FILES)

    result = _pt(monkeypatch, root, ["size", "--json"])
    assert result.exit_code == 0, result.output
    payload = _json(result.output)
    row = payload["rows"][0]
    assert (row["project"], row["code"], row["tests"], row["doc_files"], row["doc_lines"]) == (
        "proj", 9, 6, 2, 5)
    assert payload["baseline_date"] is None and row["score"] is None
    assert "none yet" in _pt(monkeypatch, root, ["size"]).output

    # The first snapshot becomes the baseline and scores itself 0.
    result = _pt(monkeypatch, root, ["size", "--json", "--snapshot"])
    assert result.exit_code == 0, result.output
    payload = _json(result.output)
    assert payload["snapshot_stored"] is True
    assert payload["baseline_date"] is not None
    assert payload["rows"][0]["score"] == 0.0

    commit(repo, {"app.py": "a\n"}, "2026-02-01T00:00:00+0000")  # code 9 -> 7
    payload = _json(_pt(monkeypatch, root, ["size", "--json"]).output)
    assert payload["rows"][0]["code"] == 7
    assert payload["rows"][0]["score"] == pytest.approx(2 / 14 * 100)
    assert payload["total"]["score"] == pytest.approx(2 / 14 * 100)

    text = _pt(monkeypatch, root, ["size"])
    assert text.exit_code == 0
    assert f"baseline: {payload['baseline_date']}" in text.output
    assert "TOTAL code 7" in text.output and "+14.3" in text.output

    # A later snapshot appends a run and leaves the baseline alone.
    assert _pt(monkeypatch, root, ["size", "--snapshot"]).exit_code == 0
    assert _runs(conn) == 2
    assert _json(_pt(monkeypatch, root, ["size", "--json"]).output)["rows"][0]["score"] == (
        pytest.approx(2 / 14 * 100))


def test_pt_size_reports_failing_repo_and_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    make_repo(root, "good", {"a.py": "x\n"})
    (root / "bad" / ".git").mkdir(parents=True)
    result = _pt(monkeypatch, root, ["size", "--json"])
    assert result.exit_code == 1
    assert "bad" in result.output


def test_pt_size_snapshot_stores_nothing_when_a_repo_fails(tmp_path: Path, monkeypatch, conn) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    make_repo(root, "good", {"a.py": "x\n"})
    (root / "bad" / ".git").mkdir(parents=True)
    result = _pt(monkeypatch, root, ["size", "--json", "--snapshot"])
    assert result.exit_code == 1
    assert "Snapshot NOT stored" in result.output
    assert _json(result.output)["snapshot_stored"] is False
    assert _count(conn) == 0


def test_pt_size_missing_table_fails_before_scanning(tmp_path: Path, monkeypatch, conn) -> None:
    conn.execute("DROP TABLE codebase_size_snapshots")
    conn.commit()

    def boom(*_a, **_k):
        raise AssertionError("scan must not run when the table is missing")

    monkeypatch.setattr(cs, "scan_portfolio", boom)
    result = _pt(monkeypatch, tmp_path, ["size", "--json"])
    assert result.exit_code != 0
    assert "pt db migrate" in result.output


def test_pt_size_with_no_repos_is_an_error_and_stores_nothing(tmp_path: Path, monkeypatch, conn) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    result = _pt(monkeypatch, root, ["size", "--snapshot"])
    assert result.exit_code != 0
    assert "no git repos found" in result.output
    assert _count(conn) == 0


def test_pt_size_with_a_missing_projects_root_is_a_clear_error(tmp_path: Path, monkeypatch, conn) -> None:
    result = _pt(monkeypatch, tmp_path / "does-not-exist", ["size", "--snapshot"])
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "Traceback" not in result.output
    assert _count(conn) == 0


def test_total_score_counts_only_repos_in_the_baseline_run(tmp_path: Path, monkeypatch, conn) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    old = make_repo(root, "old", {"a.py": "x\n" * 10, "README.md": "d\n" * 10})  # lean 20
    assert _pt(monkeypatch, root, ["size", "--snapshot"]).exit_code == 0     # baseline: old only
    commit(old, {"a.py": "x\n" * 5}, "2026-02-01T00:00:00+0000")               # lean 15
    make_repo(root, "new", {"b.py": "y\n" * 100})                              # not in baseline
    payload = _json(_pt(monkeypatch, root, ["size", "--json"]).output)
    scores = {r["project"]: r["score"] for r in payload["rows"]}
    assert scores["new"] is None
    assert scores["old"] == pytest.approx(25.0)
    # The new repo's 100 lines neither count against nor toward the total.
    assert payload["total"]["score"] == pytest.approx(25.0)
    assert payload["total"]["code"] == 105
