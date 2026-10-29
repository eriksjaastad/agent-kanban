"""Tests for dashboard.brain_stats and the /api/tool-stats and /api/bash-stats endpoints."""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dashboard_app
from dashboard.brain_stats import bash_stats, tool_stats

TODAY = date(2026, 4, 21)


def _brain_db(tmp_path: Path, ddl: str, insert: str, rows: list) -> Path:
    path = tmp_path / "brain.db"
    with sqlite3.connect(path) as conn:
        conn.execute(ddl)
        conn.executemany(insert, rows)
    return path


def test_tool_stats_counts_one_tool_per_day_project_and_model(tmp_path: Path):
    brain_db = _brain_db(
        tmp_path,
        "CREATE TABLE tool_invocations (tool_name TEXT, project TEXT, model TEXT, invoked_at TEXT)",
        "INSERT INTO tool_invocations VALUES (?, ?, ?, ?)",
        [
            ("WebSearch", "pt", "opus", "2026-04-19T09:00:00Z"),
            ("WebSearch", "pt", "sonnet", "2026-04-19T10:00:00+00:00"),
            ("WebSearch", None, "opus", "2026-04-21T11:00:00Z"),
            ("WebSearch", "pt", None, "2026-04-20T09:00:00Z"),
            ("WebFetch", "pt", "opus", "2026-04-20T09:00:00Z"),  # other tool
            ("WebSearch", "pt", "opus", "2026-04-18T09:00:00Z"),  # before the window
            ("WebSearch", "pt", "opus", "2026-04-22T01:00:00Z"),  # after it
        ],
    )

    assert tool_stats(brain_db, TODAY, "WebSearch", 3, None) == {
        "by_date": [
            {"date": "2026-04-19", "total": 2},
            {"date": "2026-04-20", "total": 1},
            {"date": "2026-04-21", "total": 1},
        ],
        "by_project": [
            {"date": "2026-04-19", "project": "pt", "count": 2},
            {"date": "2026-04-20", "project": "pt", "count": 1},
            {"date": "2026-04-21", "project": "unknown", "count": 1},
        ],
        "by_model": [
            {"date": "2026-04-19", "model": "opus", "count": 1},
            {"date": "2026-04-19", "model": "sonnet", "count": 1},
            {"date": "2026-04-20", "model": "unknown", "count": 1},
            {"date": "2026-04-21", "model": "opus", "count": 1},
        ],
        "projects": ["pt", "unknown"],
        "models": ["opus", "sonnet", "unknown"],
    }
    scoped = tool_stats(brain_db, TODAY, "WebSearch", 3, "sonnet")
    assert [d["total"] for d in scoped["by_date"]] == [1, 0, 0]
    assert scoped["by_project"] == [{"date": "2026-04-19", "project": "pt", "count": 1}]
    assert [d["total"] for d in tool_stats(brain_db, TODAY, "WebSearch", 3, "unknown")["by_date"]] == [0, 1, 0]


def test_bash_stats_error_rate_per_day_and_project(tmp_path: Path):
    brain_db = _brain_db(
        tmp_path,
        "CREATE TABLE bash_calls (project TEXT, is_error INTEGER, invoked_at TEXT)",
        "INSERT INTO bash_calls VALUES (?, ?, ?)",
        [("pt", 1, "2026-04-20T10:00:00Z")]
        + [("pt", 0, "2026-04-20T10:00:00Z")] * 15  # 1 of 16 = 6.25%, SQLite rounds to 6.3
        + [("ai-metrics", 1, "2026-04-20T11:00:00Z"), (None, 0, "2026-04-21T09:00:00Z")]
        + [("pt", 1, "2026-04-18T09:00:00Z")],  # before the window
    )

    assert bash_stats(brain_db, TODAY, 2) == {
        "by_date": [
            {"date": "2026-04-20", "error_rate": 11.8, "total": 17, "errors": 2},
            {"date": "2026-04-21", "error_rate": 0.0, "total": 1, "errors": 0},
        ],
        "by_project": [
            {"date": "2026-04-20", "project": "ai-metrics", "error_rate": 100.0},
            {"date": "2026-04-20", "project": "pt", "error_rate": 6.3},
            {"date": "2026-04-21", "project": "unknown", "error_rate": 0.0},
        ],
        "projects": ["ai-metrics", "pt", "unknown"],
    }


def test_window_ends_on_the_utc_date_the_store_stamps(memory_store, monkeypatch):
    class EveningInPacific(datetime):
        """18:00 local on Apr 21 is already Apr 22 in UTC."""

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 22, 1, 0, tzinfo=timezone.utc) if tz else datetime(2026, 4, 21, 18, 0)

    monkeypatch.setattr(dashboard_app, "datetime", EveningInPacific)
    _brain_db(
        memory_store.root,
        "CREATE TABLE tool_invocations (tool_name TEXT, project TEXT, model TEXT, invoked_at TEXT)",
        "INSERT INTO tool_invocations VALUES (?, ?, ?, ?)",
        [("WebSearch", "pt", "opus", "2026-04-22T00:30:00Z")],
    )

    response = TestClient(dashboard_app.app).get("/api/tool-stats?days=1")

    assert response.json()["by_date"] == [{"date": "2026-04-22", "total": 1}]


@pytest.mark.parametrize("make_db", [False, True], ids=["no brain.db", "no table"])
@pytest.mark.parametrize("url", ["/api/tool-stats", "/api/bash-stats"])
def test_unreadable_brain_db_is_a_503_naming_the_file(memory_store, make_db, url):
    brain_db = memory_store.root / "brain.db"
    if make_db:
        sqlite3.connect(brain_db).close()

    response = TestClient(dashboard_app.app).get(url)

    assert response.status_code == 503, response.text
    assert response.json()["detail"].startswith(f"Cannot read {brain_db}: ")
