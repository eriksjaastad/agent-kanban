"""/api/stats/sites (#8868): each site's latest run of any age, latest numbers, 30-day baseline, staleness."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dashboard.routes import site_stats  # noqa: E402
from dashboard.routes.site_stats import site_summaries  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402

A = {"gsc": {"metrics": {"impressions": 96}}}
B = {"gsc": {"metrics": {"impressions": 63}}}


def row(site, ran_at, exit_code, sources):
    return {"site": site, "ran_at": ran_at, "exit_code": exit_code,
            "sources": None if sources is None else json.dumps(sources)}


def test_latest_numbers_with_the_oldest_in_window_as_baseline_per_site_sorted():
    rows = [row("muffin", "2026-10-01", 2, A), row("muffin", "2026-11-01", 2, A), row("alpha", "2026-11-01", 0, B),
            row("muffin", "2026-11-02", 2, B), row("muffin", "2026-11-03", 2, B)]
    assert site_summaries(rows, "2026-10-04", "2026-11-01") == [
        {"site": "alpha", "ran_at": "2026-11-01", "exit_code": 0, "collected_at": "2026-11-01",
         "sources": B, "baseline_at": "2026-11-01", "baseline": B, "stale": False},
        {"site": "muffin", "ran_at": "2026-11-03", "exit_code": 2, "collected_at": "2026-11-03",
         "sources": B, "baseline_at": "2026-11-01", "baseline": A, "stale": False}]


def test_a_site_whose_collector_stopped_stays_listed_stale_without_a_baseline():
    [site] = site_summaries([row("h", "2026-09-01", 2, A)], "2026-10-04", "2026-11-01")
    assert (site["ran_at"], site["sources"], site["baseline"], site["stale"]) == ("2026-09-01", A, None, True)


def test_a_failed_latest_run_shows_with_the_last_collected_numbers():
    [site] = site_summaries([row("h", "2026-11-02", 2, A), row("h", "2026-11-03", 127, None)], "2026-10-04", "2026-11-01")
    assert (site["ran_at"], site["exit_code"], site["collected_at"], site["sources"]) == ("2026-11-03", 127, "2026-11-02", A)


def test_a_site_that_never_collected_has_no_numbers():
    [site] = site_summaries([row("h", "2026-11-03", 1, None)], "2026-10-04", "2026-11-01")
    assert site["sources"] is None and site["baseline"] is None and site["exit_code"] == 1


def test_route_bounds_the_baseline_to_30_days_and_staleness_to_2(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    db = DatabaseManager(db_path)
    now = datetime.now(timezone.utc)
    at = lambda days: (now - timedelta(days=days)).isoformat(timespec="seconds")  # noqa: E731
    db.site_stats_recorded("old", at(31), 2, json.dumps(A))
    db.site_stats_recorded("old", at(3), 2, json.dumps(B))
    db.site_stats_recorded("fresh", at(29), 2, json.dumps(A))
    db.site_stats_recorded("fresh", at(1), 2, json.dumps(B))
    db.site_stats_recorded("gone", at(40), 2, json.dumps(A))
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(site_stats.router)
    body = TestClient(app).get("/api/stats/sites").json()
    fresh, gone, old = body["sites"]
    assert (gone["site"], gone["ran_at"], gone["sources"], gone["baseline"], gone["stale"]) == ("gone", at(40), A, None, True)
    assert (fresh["site"], fresh["baseline"], fresh["stale"]) == ("fresh", A, False)
    assert (old["site"], old["baseline_at"], old["stale"]) == ("old", at(3), True)
