"""Tests for /api/health (#6752).

The endpoint exists because the dashboard has twice gone "alive but broken":
the process stays up while its SQLite handle rots and every request 500s with
"unable to open database file". A liveness stub would have reported green
through both incidents, so the test that actually matters here is the 503 one.
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app
from db.manager import DatabaseManager
from db.schema import create_database


def test_health_returns_200_when_db_is_reachable(tmp_path: Path, local_backend):
    response = TestClient(dashboard_app.app).get("/api/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"]["ok"] is True
    assert body["database"]["path"] == str(local_backend.db_path)
    assert body["database"]["task_count"] == 0
    assert isinstance(body["uptime_seconds"], int)
    assert "started_at" in body
    assert "dashboard_cache_age_seconds" not in body


def test_health_counts_real_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The probe must run a real query, not report a constant."""
    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    db = DatabaseManager()
    project_path = tmp_path / "demo"
    project_path.mkdir()
    db.add_project(project_id="demo", name="demo", path=str(project_path), status="active")
    db.add_task("health probe fixture", "demo")

    response = TestClient(dashboard_app.app).get("/api/health")

    assert response.status_code == 200, response.text
    assert response.json()["database"]["task_count"] == 1


def test_health_returns_503_when_db_is_unreachable(monkeypatch: pytest.MonkeyPatch):
    """The assertion that would have caught 2026-07-06 and 2026-07-08.

    A rotted handle raises exactly this on every query; health must fail with
    it rather than reporting 200 with a flag, because the watchdog only reads
    the HTTP status code.
    """
    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(dashboard_app, "DatabaseManager", _boom)

    response = TestClient(dashboard_app.app).get("/api/health")

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["database"]["ok"] is False
    assert "unable to open database file" in body["database"]["error"]


def test_health_reports_unopenable_configured_database(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker")
    monkeypatch.setenv("PT_DB_PATH", str(blocker / "tracker.db"))
    response = TestClient(dashboard_app.app).get("/api/health")
    assert response.status_code == 503, response.text
    assert response.json()["database"]["ok"] is False
    assert response.json()["database"]["error"]


def test_health_does_not_require_retired_service():
    response = TestClient(dashboard_app.app).get("/api/health")
    assert response.status_code == 200, response.text
    assert response.json()["database"]["ok"] is True


def test_watchdog_probes_health_endpoint():
    """The watchdog must not use /api/alerts (a full board scan) as a probe."""
    watchdog = (Path(__file__).parent.parent / "scripts" / "dashboard_watchdog.sh").read_text()

    assert 'URL="${PT_DASHBOARD_URL:-http://localhost:8000/api/health}"' in watchdog
    assert "/api/alerts}" not in watchdog
