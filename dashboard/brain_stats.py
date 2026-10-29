"""Read-only usage stats from the memory store's brain.db for the /agentic page.

The memory store owns the schema; this module only reads its ``tool_invocations``
and ``bash_calls`` tables. A day is the first ten characters of
``invoked_at``, which the memory store writes in UTC. Any failure to read (no
brain.db, a missing table or column) raises BrainStatsUnavailable instead of
returning empty series, so the page shows the error, not a "no data yet" chart.
"""

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

_DAY = "substr(invoked_at, 1, 10)"


class BrainStatsUnavailable(Exception):
    """brain.db could not be read."""


def _window(days: int, today: date) -> list[str]:
    """ISO dates of the last ``days`` days ending ``today``; days is clamped to 1..365."""
    days = min(max(days, 1), 365)
    return [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def _read(brain_db: Path, sql: str, params: list) -> list[sqlite3.Row]:
    try:
        with closing(sqlite3.connect(f"file:{brain_db}?mode=ro", uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise BrainStatsUnavailable(f"Cannot read {brain_db}: {exc}") from exc


def _rate(errors: int, total: int) -> float:
    """Percent to one decimal, halves rounded up (as SQLite's ROUND, not Python's round)."""
    return (errors * 2000 // total + 1) // 2 / 10 if total else 0.0


def tool_stats(brain_db: Path, today: date, tool: str, days: int, model: Optional[str]) -> dict:
    """Calls of one tool per day: in total, per project and per model.

    ``model``, when given, scopes every series to that model; "unknown" picks
    calls with no model recorded, as the series label them. ``by_date``
    has one entry for every day in the window; the per-project and per-model
    series list only days with calls.
    """
    dates = _window(days, today)
    where = f"tool_name = ? AND {_DAY} BETWEEN ? AND ?"
    params = [tool, dates[0], dates[-1]]
    if model is not None:
        where += " AND COALESCE(model, 'unknown') = ?"
        params.append(model)
    rows = _read(
        brain_db,
        f"""
        SELECT {_DAY} AS date, COALESCE(project, 'unknown') AS project,
               COALESCE(model, 'unknown') AS model, COUNT(*) AS count
        FROM tool_invocations WHERE {where}
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
        """,
        params,
    )

    totals = dict.fromkeys(dates, 0)
    by_project: dict[tuple[str, str], int] = {}
    by_model: dict[tuple[str, str], int] = {}
    for row in rows:
        totals[row["date"]] += row["count"]
        key = (row["date"], row["project"])
        by_project[key] = by_project.get(key, 0) + row["count"]
        key = (row["date"], row["model"])
        by_model[key] = by_model.get(key, 0) + row["count"]

    return {
        "by_date": [{"date": d, "total": n} for d, n in totals.items()],
        "by_project": [
            {"date": d, "project": p, "count": n} for (d, p), n in sorted(by_project.items())
        ],
        "by_model": [
            {"date": d, "model": m, "count": n} for (d, m), n in sorted(by_model.items())
        ],
        "projects": sorted({p for _, p in by_project}),
        "models": sorted({m for _, m in by_model}),
    }


def bash_stats(brain_db: Path, today: date, days: int) -> dict:
    """Bash error rate (percent of calls with is_error) per day, overall and per project.

    ``by_date`` has one entry for every day in the window; ``by_project``
    lists only days with calls.
    """
    dates = _window(days, today)
    rows = _read(
        brain_db,
        f"""
        SELECT {_DAY} AS date, COALESCE(project, 'unknown') AS project,
               COUNT(*) AS total, COALESCE(SUM(is_error), 0) AS errors
        FROM bash_calls WHERE {_DAY} BETWEEN ? AND ?
        GROUP BY 1, 2 ORDER BY 1, 2
        """,
        [dates[0], dates[-1]],
    )

    totals = {d: [0, 0] for d in dates}
    for row in rows:
        totals[row["date"]][0] += row["total"]
        totals[row["date"]][1] += row["errors"]

    return {
        "by_date": [
            {"date": d, "error_rate": _rate(errors, total), "total": total, "errors": errors}
            for d, (total, errors) in totals.items()
        ],
        "by_project": [
            {"date": r["date"], "project": r["project"], "error_rate": _rate(r["errors"], r["total"])}
            for r in rows
        ],
        "projects": sorted({r["project"] for r in rows}),
    }
