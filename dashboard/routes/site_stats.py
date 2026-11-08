"""Website stats per site from `pt site-stats run` (#8868).

Each site's latest run of any age (exit code and time), its latest collected
sources, and the oldest collected sources in the last 30 days as the trend's
baseline. A run that collected nothing (sources NULL) still counts as the
latest run, so a failing collector shows instead of hiding behind older
numbers, and a collector that stopped running keeps its site listed with its
last run's time for the panel to flag.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from db.manager import DatabaseManager

router = APIRouter()
DAYS = 30
STALE_AFTER = timedelta(days=2)  # daily runs; a closed laptop catches up on wake


def site_summaries(rows: list, since: str, stale_before: str) -> list:
    """Rows oldest first -> one entry per site, sorted by site.

    Baselines come from rows at or after ``since``; a site whose latest run is
    before ``stale_before`` is marked stale (the collector stopped running).
    """
    sites: dict = {}
    for row in rows:
        entry = sites.setdefault(row["site"], {"site": row["site"], "collected_at": None, "sources": None,
                                               "baseline_at": None, "baseline": None})
        entry["ran_at"], entry["exit_code"] = row["ran_at"], row["exit_code"]
        if row["sources"] is not None:
            sources = json.loads(row["sources"])
            if entry["baseline"] is None and row["ran_at"] >= since:
                entry["baseline_at"], entry["baseline"] = row["ran_at"], sources
            entry["collected_at"], entry["sources"] = row["ran_at"], sources
    for entry in sites.values():
        entry["stale"] = entry["ran_at"] < stale_before
    return [sites[name] for name in sorted(sites)]


@router.get("/api/stats/sites")
def get_site_stats():
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=DAYS)).isoformat(timespec="seconds")
    stale_before = (now - STALE_AFTER).isoformat(timespec="seconds")
    # One row per site per day: every row is small enough to read.
    return {"days": DAYS, "sites": site_summaries(DatabaseManager().site_stats_since(""), since, stale_before)}
