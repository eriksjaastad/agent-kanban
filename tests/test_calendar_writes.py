"""Calendar manager and dashboard calendar endpoints (#7899).

The dashboard's create endpoint once passed a `machine` argument the manager
no longer accepted, so it 500ed before reaching the database. These tests cover
that path plus plain create / done / link writes.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app  # noqa: E402
from db.backend_calendar_manager import CalendarManager  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402


def test_calendar_writes_create_done_and_link(tmp_path: Path) -> None:
    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    cm = CalendarManager(db_path)

    event_id = cm.add_event(title="Review", event_date="2030-01-01")
    assert cm.mark_done(event_id) is True
    assert cm.get_event(event_id)["status"] == "done"

    db = DatabaseManager(db_path)
    db.add_project(project_id="p", name="P", path=str(tmp_path / "p"), status="active")
    task = db.add_task("Linked card", "p", notes="- [ ] linked")
    cm.link_task(event_id, task["id"])
    assert [t["id"] for t in cm.get_event(event_id)["linked_tasks"]] == [task["id"]]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    return TestClient(dashboard_app.app)


@pytest.mark.parametrize("machine", [None, "MacBook"])
def test_create_event_endpoint_succeeds(client: TestClient, machine) -> None:
    payload = {"title": "Ship it", "event_date": "2030-01-01"}
    if machine:
        payload["machine"] = machine
    resp = client.post("/api/calendar/events", json=payload)
    assert resp.status_code == 200, resp.text
    event_id = resp.json()["id"]
    listing = client.get("/api/calendar/events?include_all=true")
    assert listing.status_code == 200, listing.text
    assert [e["machine"] for e in listing.json()["events"]] == [machine]

    done = client.patch(f"/api/calendar/events/{event_id}/done")
    assert done.status_code == 200, done.text
