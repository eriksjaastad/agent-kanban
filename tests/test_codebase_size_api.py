"""/api/codebase-size: read the stored runs, refresh by scanning (#8083)."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dashboard.app import app  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from scripts import codebase_size as cs  # noqa: E402
from scripts.codebase_size import RepoSize  # noqa: E402

LOCAL = TestClient(app)
REMOTE = TestClient(app, client=("remote.example", 50000))


@pytest.fixture
def conn():
    db = sqlite3.connect(os.environ["PT_DB_PATH"])
    yield db
    db.close()


def _runs(conn) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT run_id) FROM codebase_size_snapshots"
    ).fetchone()[0]


def _repo(name: str, code: int, docs: int = 0, tests: int = 0, error: str | None = None) -> RepoSize:
    return RepoSize(name, code, tests, 1 if docs else 0, docs, "2026-10-01", 3, error)


def _scan_returns(monkeypatch, rows: list[RepoSize]) -> None:
    monkeypatch.setattr(cs, "scan_portfolio", lambda root=None: rows)


def test_get_with_no_runs_is_the_empty_state() -> None:
    response = LOCAL.get("/api/codebase-size")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["baseline_date"] is None and body["latest_date"] is None
    assert body["total"]["score"] is None and body["total"]["repos"] == 0
    assert body["formula"] == cs.SCORE_FORMULA


def test_get_scores_latest_run_against_baseline() -> None:
    db = DatabaseManager()
    db.save_codebase_scan([_repo("a", 80, 20), _repo("b", 50)], "2026-10-01")
    db.save_codebase_scan([_repo("a", 60, 20), _repo("b", 50), _repo("c", 10)], "2026-10-08")

    body = LOCAL.get("/api/codebase-size").json()
    assert body["baseline_date"] == "2026-10-01" and body["latest_date"] == "2026-10-08"
    rows = {r["project"]: r for r in body["rows"]}
    assert [r["project"] for r in body["rows"]] == ["a", "b", "c"]  # code descending
    assert rows["a"]["baseline_lean"] == 100 and rows["a"]["change"] == -20
    assert rows["a"]["score"] == pytest.approx(20.0)
    assert rows["b"]["change"] == 0 and rows["b"]["score"] == 0.0
    assert rows["c"]["baseline_lean"] is None
    assert rows["c"]["change"] is None and rows["c"]["score"] is None
    # c has no baseline row, so the total scores only a and b: (150 - 130) / 150
    assert body["total"]["score"] == pytest.approx(20 / 150 * 100)
    assert body["total"]["code"] == 120 and body["total"]["repos"] == 3

    expected = cs.build_report(
        [_repo("a", 60, 20), _repo("b", 50), _repo("c", 10)],
        {"a": _repo("a", 80, 20), "b": _repo("b", 50)}, "2026-10-01", "2026-10-08",
    )
    assert body["total"] == expected["total"]


def test_get_with_missing_table_is_an_error_not_an_empty_result(conn) -> None:
    conn.execute("DROP TRIGGER IF EXISTS codebase_size_snapshots_no_delete")
    conn.execute("DROP TABLE codebase_size_snapshots")
    conn.commit()
    response = LOCAL.get("/api/codebase-size")
    assert response.status_code == 500
    assert "codebase_size_snapshots" in response.json()["detail"]


def test_refresh_from_non_loopback_client_is_forbidden(monkeypatch, conn) -> None:
    monkeypatch.delenv("PT_ALLOW_REMOTE_ADMIN", raising=False)
    _scan_returns(monkeypatch, [_repo("a", 10)])
    assert REMOTE.post("/api/codebase-size/refresh").status_code == 403
    assert _runs(conn) == 0


def test_refresh_stores_one_run_and_the_first_becomes_the_baseline(monkeypatch, conn) -> None:
    _scan_returns(monkeypatch, [_repo("a", 100, 10), _repo("b", 40)])
    first = LOCAL.post("/api/codebase-size/refresh")
    assert first.status_code == 200, first.text
    body = first.json()
    assert _runs(conn) == 1
    assert body["baseline_date"] == body["latest_date"] is not None
    assert [r["score"] for r in body["rows"]] == [0.0, 0.0]

    _scan_returns(monkeypatch, [_repo("a", 80, 10), _repo("b", 40)])
    second = LOCAL.post("/api/codebase-size/refresh").json()
    assert _runs(conn) == 2
    row = next(r for r in second["rows"] if r["project"] == "a")
    assert row["change"] == -20 and row["score"] == pytest.approx(20 / 110 * 100)
    assert second == LOCAL.get("/api/codebase-size").json()


def test_refresh_with_a_failing_repo_is_502_and_stores_nothing(monkeypatch, conn) -> None:
    _scan_returns(monkeypatch, [_repo("a", 10), _repo("broken", 0, error="git ls-files exited 128")])
    response = LOCAL.post("/api/codebase-size/refresh")
    assert response.status_code == 502
    assert "broken" in response.json()["detail"] and "128" in response.json()["detail"]
    assert _runs(conn) == 0


def _missing_projects_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path / "absent"))


def _no_repos(monkeypatch, tmp_path: Path) -> None:
    _scan_returns(monkeypatch, [])


def _scan_raises(monkeypatch, tmp_path: Path) -> None:
    def boom(root=None):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(cs, "scan_portfolio", boom)


@pytest.mark.parametrize(
    "setup, detail",
    [
        (_missing_projects_root, "does not exist"),
        (_no_repos, "No git repos found"),
        (_scan_raises, "disk on fire"),
    ],
    ids=["missing_projects_root", "no_repos", "scan_itself_raises"],
)
def test_failed_refresh_is_500_and_stores_nothing(
    monkeypatch, tmp_path: Path, conn, setup, detail
) -> None:
    setup(monkeypatch, tmp_path)
    response = LOCAL.post("/api/codebase-size/refresh")
    assert response.status_code == 500
    assert detail in response.json()["detail"]
    assert _runs(conn) == 0


def test_both_runs_come_from_one_consistent_snapshot(monkeypatch, conn) -> None:
    # Force the race Codex found: a first run commits on another connection
    # between the baseline read and the latest-run read.
    import db.codebase_size as storage

    real_read = storage._read_run
    calls = []

    def read_then_commit_elsewhere(c, newest):
        result = real_read(c, newest)
        if not calls:
            DatabaseManager().save_codebase_scan([_repo("a", 10)], "2026-10-08")
        calls.append(newest)
        return result

    monkeypatch.setattr(storage, "_read_run", read_then_commit_elsewhere)
    base_date, base, latest_date, latest = DatabaseManager().codebase_runs()
    assert calls == [False, True]
    assert _runs(conn) == 1  # the concurrent run did commit
    # Both reads saw the snapshot from before it: no run on either side.
    assert (base_date, base, latest_date, latest) == (None, {}, None, {})

    monkeypatch.setattr(storage, "_read_run", real_read)
    body = LOCAL.get("/api/codebase-size").json()
    assert body["baseline_date"] == "2026-10-08"
    assert body["rows"][0]["score"] == 0.0


def test_overlapping_refresh_is_refused_with_409_and_stores_nothing(monkeypatch, conn) -> None:
    import dashboard.routes.agentic as app_module

    _scan_returns(monkeypatch, [_repo("a", 10)])
    assert app_module._codebase_refresh_lock.acquire(blocking=False)
    try:
        response = LOCAL.post("/api/codebase-size/refresh")
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        assert _runs(conn) == 0
    finally:
        app_module._codebase_refresh_lock.release()
    assert LOCAL.post("/api/codebase-size/refresh").status_code == 200
    assert _runs(conn) == 1


def test_lock_is_released_after_a_failed_refresh(monkeypatch, conn) -> None:
    _scan_returns(monkeypatch, [_repo("broken", 0, error="git exited 128")])
    assert LOCAL.post("/api/codebase-size/refresh").status_code == 502
    _scan_returns(monkeypatch, [_repo("a", 10)])
    assert LOCAL.post("/api/codebase-size/refresh").status_code == 200


def test_refresh_reports_the_run_it_stored(monkeypatch, conn) -> None:
    _scan_returns(monkeypatch, [_repo("a", 10), _repo("b", 5)])
    first = LOCAL.post("/api/codebase-size/refresh").json()
    assert first["baseline_date"] == first["latest_date"]
    assert {r["project"]: r["score"] for r in first["rows"]} == {"a": 0.0, "b": 0.0}
    _scan_returns(monkeypatch, [_repo("a", 8)])
    second = LOCAL.post("/api/codebase-size/refresh").json()
    assert [r["project"] for r in second["rows"]] == ["a"]
    assert second["rows"][0]["change"] == -2
