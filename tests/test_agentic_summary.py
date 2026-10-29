"""Tests for /api/agentic/summary."""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app
from fastapi.testclient import TestClient

from db.backend_manager import DatabaseManager as BackendDatabaseManager


def _setup_db(db: BackendDatabaseManager) -> dict[str, int]:
    task_ids: dict[str, int] = {}

    for project_id in ("project-tracker", "image-workflow"):
        db.add_project(
            project_id=project_id,
            name=project_id,
            path=f"./{project_id}",
            status="active",
        )
        task = db.add_task(text=f"{project_id} task", project_id=project_id, status="Review")
        task_ids[project_id] = task["id"]

    return task_ids


def _insert_history(
    db: BackendDatabaseManager, task_id: int, project_id: str, rows: list[tuple[str, str, str]]
) -> None:
    with db._get_conn() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO task_history (task_id, project_id, event_type, old_status, new_status, timestamp)
            VALUES (?, ?, 'status_changed', ?, ?, ?)
            """,
            [(task_id, project_id, old_status, new_status, timestamp) for timestamp, old_status, new_status in rows],
        )
        conn.commit()


def _freeze_now(monkeypatch: pytest.MonkeyPatch, dashboard_app_module, frozen_now: datetime) -> None:
    class FrozenDateTime:
        @classmethod
        def now(cls) -> datetime:
            return frozen_now

    monkeypatch.setattr(dashboard_app_module, "datetime", FrozenDateTime)


def _patch_markers(monkeypatch: pytest.MonkeyPatch, dashboard_app_module, content: str | None) -> None:
    real_exists = dashboard_app_module.Path.exists
    real_read_text = dashboard_app_module.Path.read_text

    def fake_exists(self: Path) -> bool:
        if self.name == "agentic_markers.json":
            return content is not None
        return real_exists(self)

    def fake_read_text(self: Path, *args, **kwargs) -> str:
        if self.name == "agentic_markers.json":
            return content or ""
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(dashboard_app_module.Path, "exists", fake_exists)
    monkeypatch.setattr(dashboard_app_module.Path, "read_text", fake_read_text)


def test_agentic_summary_aggregates_rates_series_and_markers(local_backend, monkeypatch: pytest.MonkeyPatch):
    db = local_backend
    task_ids = _setup_db(db)
    _freeze_now(monkeypatch, dashboard_app, datetime(2026, 3, 10, 12, 0, 0))
    _patch_markers(
        monkeypatch,
        dashboard_app,
        json.dumps([
            {"date": "2026-03-08", "label": "Workflow change"},
            {"date": "2026-02-20", "label": "Out of range"},
            {"date": "2026-03-09"},
        ]),
    )

    _insert_history(db, task_ids["project-tracker"], "project-tracker", [
        ("2026-03-08 09:00:00", "Review", "In Progress"),
        ("2026-03-08 10:00:00", "Review", "Done"),
        ("2026-03-08 11:00:00", "In Progress", "Review"),
        ("2026-03-10 14:00:00", "Review", "In Progress"),
        ("2026-03-01 08:00:00", "Review", "In Progress"),
    ])

    response = TestClient(dashboard_app.app).get("/api/agentic/summary?days=3")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["summary"] == {
        "review_bounces": 2,
        "review_promotions": 1,
        "review_entries": 1,
        "bounce_rate": pytest.approx(2 / 3),
        "promotion_rate": pytest.approx(1 / 3),
    }
    assert payload["series"] == [
        {"date": "2026-03-08", "review_bounces": 1, "review_promotions": 1, "review_entries": 1},
        {"date": "2026-03-09", "review_bounces": 0, "review_promotions": 0, "review_entries": 0},
        {"date": "2026-03-10", "review_bounces": 1, "review_promotions": 0, "review_entries": 0},
    ]
    assert payload["markers"] == [{"date": "2026-03-08", "label": "Workflow change"}]
    assert payload["markers_error"] is None
    assert payload["date_range"] == {"start": "2026-03-08", "end": "2026-03-10"}


def test_agentic_summary_filters_by_project(local_backend, monkeypatch: pytest.MonkeyPatch):
    db = local_backend
    task_ids = _setup_db(db)
    _freeze_now(monkeypatch, dashboard_app, datetime(2026, 3, 10, 12, 0, 0))
    _patch_markers(monkeypatch, dashboard_app, None)

    _insert_history(db, task_ids["project-tracker"], "project-tracker", [
        ("2026-03-10 09:00:00", "Review", "In Progress"),
        ("2026-03-10 10:00:00", "Review", "Done"),
    ])
    _insert_history(db, task_ids["image-workflow"], "image-workflow", [
        ("2026-03-10 11:00:00", "Review", "In Progress"),
        ("2026-03-10 12:00:00", "In Progress", "Review"),
    ])

    response = TestClient(dashboard_app.app).get("/api/agentic/summary?days=1&project_id=project-tracker")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["project_id"] == "project-tracker"
    assert payload["summary"]["review_bounces"] == 1
    assert payload["summary"]["review_promotions"] == 1
    assert payload["summary"]["review_entries"] == 0


def test_agentic_summary_handles_invalid_days_and_malformed_markers(local_backend, monkeypatch: pytest.MonkeyPatch):
    _setup_db(local_backend)
    _freeze_now(monkeypatch, dashboard_app, datetime(2026, 3, 10, 12, 0, 0))
    _patch_markers(monkeypatch, dashboard_app, "{not-json")

    response = TestClient(dashboard_app.app).get("/api/agentic/summary?days=0")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["summary"] == {
        "review_bounces": 0,
        "review_promotions": 0,
        "review_entries": 0,
        "bounce_rate": 0.0,
        "promotion_rate": 0.0,
    }
    assert payload["markers"] == []
    assert payload["markers_error"] is not None
    assert "invalid JSON" in payload["markers_error"]
    assert payload["date_range"] == {"start": "2026-02-09", "end": "2026-03-10"}
    assert len(payload["series"]) == 30


def test_agentic_summary_reports_markers_error_for_non_utf8_markers_file(
    local_backend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    _setup_db(local_backend)
    _freeze_now(monkeypatch, dashboard_app, datetime(2026, 3, 10, 12, 0, 0))
    markers_file = tmp_path / "agentic_markers.json"
    markers_file.write_bytes(b"\xff\xfe[")
    monkeypatch.setattr(dashboard_app, "MARKERS_PATH", markers_file)

    response = TestClient(dashboard_app.app).get("/api/agentic/summary?days=3")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["markers"] == []
    assert payload["markers_error"] is not None
    assert "not valid UTF-8" in payload["markers_error"]
