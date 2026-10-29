"""Idea and calendar-event ids must not lose precision on the wire (#7826).

Follow-up to #7824 (tasks/attachments). pt_next_id (#6044) also generates the
ids of ``ideas`` and ``calendar_events``; a bare JSON number above 2^53-1 is
silently rounded by every browser's ``JSON.parse``. The API must emit these
ids as decimal strings. Path params stay ``int``: FastAPI coerces an
exact-digit string to a Python int losslessly.

Wire assertions use the RAW response text, not ``response.json()``, which
would hide the regression the same way Python's json hides it.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app  # noqa: E402
from db.calendar_manager import CalendarManager  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402

BIG_ID = 98969975881101312
assert BIG_ID > 2**53 - 1


def _compact(text: str) -> str:
    return text.replace(" ", "").replace("\n", "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id="project-tracker",
        name="Project Tracker",
        path=str(tmp_path / "project-tracker"),
        status="active",
    )
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    # Consecutive oversized ids; a constant would trip UNIQUE on history rows.
    counter = itertools.count(BIG_ID)
    gen = lambda _db_path: next(counter)  # noqa: E731
    monkeypatch.setattr("db.backend_manager.pt_next_id", gen)
    monkeypatch.setattr("db.backend_calendar_manager.pt_next_id", gen)
    return db, TestClient(dashboard_app.app)


def test_idea_ids_are_quoted_strings_and_patch_delete_hit_exact_row(env):
    db, client = env
    first = db.add_idea("first")
    second = db.add_idea("second")
    assert first["id"] == BIG_ID and second["id"] == BIG_ID + 1

    created = client.post("/api/ideas", json={"text": "third"})
    assert created.status_code == 201, created.text
    assert f'"id":"{BIG_ID + 2}"' in _compact(created.text)

    listing = client.get("/api/ideas")
    assert f'"id":"{BIG_ID}"' in _compact(listing.text)
    assert f'"id":"{BIG_ID + 1}"' in _compact(listing.text)

    # Adjacent ids: a rounded id would hit the wrong row.
    patched = client.patch(f"/api/ideas/{BIG_ID + 1}", json={"text": "changed"})
    assert patched.status_code == 200, patched.text
    assert f'"id":"{BIG_ID + 1}"' in _compact(patched.text)
    assert db.get_idea(BIG_ID + 1)["text"] == "changed"
    assert db.get_idea(BIG_ID)["text"] == "first"

    deleted = client.delete(f"/api/ideas/{BIG_ID + 1}")
    assert deleted.status_code == 200, deleted.text
    assert db.get_idea(BIG_ID + 1) is None
    assert db.get_idea(BIG_ID)["text"] == "first"


def _add_event(title: str, date: str) -> int:
    cm = CalendarManager()
    return cm.add_event(title=title, event_date=date)


def test_calendar_create_response_id_is_quoted(env):
    _db, client = env
    created = client.post(
        "/api/calendar/events", json={"title": "A", "event_date": "2030-01-01"}
    )
    assert created.status_code == 200, created.text
    assert f'"id":"{BIG_ID}"' in _compact(created.text)


def test_calendar_event_ids_are_quoted_strings_and_done_hits_exact_row(env):
    _db, client = env
    first = _add_event("A", "2030-01-01")
    second = _add_event("B", "2030-01-02")
    assert (first, second) == (BIG_ID, BIG_ID + 1)

    listing = client.get("/api/calendar/events?days=730&include_all=true")
    assert listing.status_code == 200, listing.text
    assert f'"id":"{BIG_ID}"' in _compact(listing.text)
    assert f'"id":"{BIG_ID + 1}"' in _compact(listing.text)

    done = client.patch(f"/api/calendar/events/{BIG_ID + 1}/done")
    assert done.status_code == 200, done.text
    cm = CalendarManager()
    assert cm.get_event(BIG_ID + 1)["status"] == "done"
    assert cm.get_event(BIG_ID)["status"] == "active"
