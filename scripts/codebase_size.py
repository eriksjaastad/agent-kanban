"""Codebase size scanner and optimization score (#8083).

Measures how much code and documentation each git repo under the projects root
carries and scores a repo against its baseline. Runs are stored by
``db.codebase_size.CodebaseSizeMixin`` in ``codebase_size_snapshots``; this
module has no database code.

Optimization score: ``SCORE_FORMULA`` below is the single statement of the
formula; the CLI help and the /codebase page both render it. In short,
``score = (baseline_lean - current_lean) / baseline_lean * 100`` where ``lean``
= non-test code lines + markdown doc lines, and the baseline is the repo's row
in the earliest stored ``pt size --snapshot`` run. Positive = leaner. Tests are
shown but excluded so cutting tests cannot raise the score.
Doc LINES (not files) are counted so moving a doc into docstrings scores
neutral, while deleting redundant docs or code scores positive. The score is
``None`` when the repo has no row in the baseline run or ``baseline_lean`` is 0.

What a scan measures: each repo's checked-out working tree, best effort, with
the same CODE / SKIP / TEST rules and line-count rule as the Architect's
2026-10-08 portfolio survey. A repo sitting on a feature branch at scan time is
measured as that branch; that is a known, accepted inaccuracy, not a defect.

Failure handling: every git call has a timeout and a checked return code. A
repo whose git call fails is reported with ``error`` set and is never counted
as zero lines. A file git lists but that is gone from disk is an expected
absence and is skipped; nothing else is swallowed. A repo with no commits yet
is measured with no activity. A missing projects root raises
ProjectsRootMissing rather than reading as an empty portfolio.
"""

from __future__ import annotations

import io
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

CODE = re.compile(r"\.(py|js|ts|tsx|jsx|sh|swift|go|rs|mjs|cjs)$")
SKIP = re.compile(
    r"(^|/)(node_modules|vendor|dist|build|\.venv|venv|migrations|fixtures|data)/"
    r"|\.min\.js$|package-lock|\.lock$"
)
TEST = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.|\.test\.|\.spec\."
)

SCORE_FORMULA = (
    "score = (baseline_lean \u2212 current_lean) / baseline_lean \u00d7 100, "
    "where lean = non-test code lines + markdown doc lines; "
    "positive = leaner; tests shown but not scored"
)

GIT_TIMEOUT = 60
RECENT_DAYS = 90


class GitError(RuntimeError):
    """A git call timed out or exited nonzero."""


@dataclass(frozen=True)
class RepoSize:
    project: str
    code: int = 0
    tests: int = 0
    doc_files: int = 0
    doc_lines: int = 0
    last_commit: Optional[str] = None
    commits_90d: Optional[int] = None
    error: Optional[str] = None

    @property
    def lean(self) -> int:
        return self.code + self.doc_lines

    def as_dict(self) -> dict:
        return {
            "project": self.project,
            "code": self.code,
            "tests": self.tests,
            "doc_files": self.doc_files,
            "doc_lines": self.doc_lines,
            "last_commit": self.last_commit,
            "commits_90d": self.commits_90d,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# git + counting helpers
# ---------------------------------------------------------------------------


def _git(path: Path, *args: str, input: Optional[bytes] = None,
         timeout: int = GIT_TIMEOUT) -> bytes:
    cmd = ["git", "-C", str(path), *args]
    try:
        proc = subprocess.run(
            cmd, input=input, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise GitError(f"git {args[0]} exited {proc.returncode}: {detail}")
    return proc.stdout


def count_lines(data: bytes) -> int:
    """Number of lines, counting a final unterminated line (survey rule)."""
    stream = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="ignore")
    return sum(1 for _ in stream)


def _classify(files_with_counts: Iterable[tuple[str, int]]) -> tuple[int, int, int, int]:
    """(code, tests, doc_files, doc_lines) from (path, line_count) pairs."""
    code = tests = doc_files = doc_lines = 0
    for path, n in files_with_counts:
        if path.endswith(".md"):
            doc_files += 1
            doc_lines += n
        elif TEST.search(path):
            tests += n
        else:
            code += n
    return code, tests, doc_files, doc_lines


def _wanted(path: str) -> bool:
    """Counted paths: not skipped, and either markdown or a code extension."""
    if not path or SKIP.search(path):
        return False
    return path.endswith(".md") or bool(CODE.search(path))


def _has_commits(path: Path) -> bool:
    """False for an unborn HEAD (``git init`` with no commit yet)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True, timeout=GIT_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git rev-parse timed out after {GIT_TIMEOUT}s") from exc
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False  # no HEAD commit yet: an expected state, measured as no activity
    detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
    raise GitError(f"git rev-parse exited {proc.returncode}: {detail}")


def _activity(path: Path) -> tuple[Optional[str], int]:
    if not _has_commits(path):
        return None, 0
    last = _git(path, "log", "-1", "--format=%cs").decode().strip()
    count = _git(
        path, "rev-list", "--count", f"--since={RECENT_DAYS} days ago", "HEAD"
    ).decode().strip()
    return last, int(count)


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def scan_repo(path: Path) -> RepoSize:
    """Measure the working tree of one git repo."""
    path = Path(path)
    name = path.name
    try:
        listed = _git(path, "ls-files", "-z").decode("utf-8", "replace").split("\0")
        counts: list[tuple[str, int]] = []
        for rel in listed:
            if not _wanted(rel):
                continue
            try:
                data = (path / rel).read_bytes()
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                # Listed by git, absent on disk: expected. The survey counts a
                # tracked .md without opening it, so keep it as a 0-line doc.
                if rel.endswith(".md"):
                    counts.append((rel, 0))
                continue
            counts.append((rel, count_lines(data)))
        last, c90 = _activity(path)
    except (GitError, OSError) as exc:
        return RepoSize(project=name, error=str(exc))
    code, tests, doc_files, doc_lines = _classify(counts)
    return RepoSize(name, code, tests, doc_files, doc_lines, last, c90)


class ProjectsRootMissing(FileNotFoundError):
    """The projects root does not exist or is not a directory."""


def _project_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ProjectsRootMissing(f"projects root {root} does not exist")
    return [
        p for p in sorted(root.iterdir())
        if (p / ".git").is_dir() and "-wt-" not in p.name
    ]


def scan_portfolio(root: Optional[Path] = None) -> list[RepoSize]:
    """Scan every git repo under the projects root, sorted by code descending."""
    if root is None:
        from scripts.config import projects_root
        root = projects_root()
    rows = [scan_repo(p) for p in _project_dirs(Path(root))]
    return sorted(rows, key=lambda r: (r.code, r.tests, r.doc_files, r.project), reverse=True)


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def optimization_score(baseline: Optional[RepoSize], current: RepoSize) -> Optional[float]:
    """(baseline_lean - current_lean) / baseline_lean * 100, or None."""
    if baseline is None or baseline.lean == 0:
        return None
    return (baseline.lean - current.lean) / baseline.lean * 100


def build_report(
    current: list[RepoSize],
    baseline: dict,
    baseline_date: Optional[str],
    latest_date: Optional[str] = None,
) -> dict:
    """The size report shared by ``pt size --json`` and the /codebase API.

    ``current`` rows must be error-free. Each row gains ``score``,
    ``baseline_lean`` (None when the repo is not in the baseline run) and
    ``change`` (lean - baseline_lean, None without a baseline). The total
    score sums only repos present in the baseline run with baseline lean > 0.
    """
    ordered = sorted(
        current,
        key=lambda r: (r.code, r.tests, r.doc_files, r.project),
        reverse=True,
    )
    rows = []
    for r in ordered:
        d = r.as_dict()
        base = baseline.get(r.project)
        d["score"] = optimization_score(base, r)
        d["baseline_lean"] = None if base is None else base.lean
        d["change"] = None if base is None else r.lean - base.lean
        rows.append(d)
    pairs = [
        (baseline[r.project], r)
        for r in ordered
        if r.project in baseline and baseline[r.project].lean
    ]
    base_lean = sum(b.lean for b, _ in pairs)
    total_score = (
        None if not base_lean
        else (base_lean - sum(c.lean for _, c in pairs)) / base_lean * 100
    )
    total = {
        "code": sum(r.code for r in ordered),
        "tests": sum(r.tests for r in ordered),
        "doc_files": sum(r.doc_files for r in ordered),
        "doc_lines": sum(r.doc_lines for r in ordered),
        "repos": len(ordered),
        "score": total_score,
    }
    return {
        "baseline_date": baseline_date,
        "latest_date": latest_date,
        "formula": SCORE_FORMULA,
        "rows": rows,
        "total": total,
    }
