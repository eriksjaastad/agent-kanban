"""Calendar trim (#8093 PR 8a): pin the surviving surface and the date/time rules.

The reminder poller never ran (cron does not run on this Mac) and the 7am
digest now carries calendar deadlines, so the poller path, its CLI commands,
its API endpoint and its model seat were cut. These tests pin what is left.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard.app as dashboard_app  # noqa: E402
from db.schema import create_database  # noqa: E402

EXPECTED_CALENDAR_COMMANDS = {
    "add", "add-cron", "cancel", "crons", "done", "now", "show", "update",
}

BAD_DATES = ["2026-13-01", "tomorrow", "2026-02-30", "2026-1-5", "", "2026-01-01T00:00"]
BAD_TIMES = ["25:00", "9am", "9:00", "12:60", "12:00:00"]


@pytest.fixture
def tracker(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "tracker.db"
        create_database(db_path)
        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        monkeypatch.setenv("PROJECTS_ROOT", tmp)
        for mod in ("scripts.pt", "scripts.config", "db.manager"):
            sys.modules.pop(mod, None)
        from scripts.pt import cli as pt_cli

        yield {"cli": pt_cli, "db_path": db_path}


@pytest.fixture
def client(tracker) -> TestClient:
    return TestClient(dashboard_app.app)


def _event_rows(db_path: Path) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT event_date, event_time FROM calendar_events").fetchall()


# --- surface ---------------------------------------------------------------

def test_calendar_subcommand_set_is_the_trimmed_set(tracker):
    from scripts.pt import calendar_group

    assert set(calendar_group.commands) == EXPECTED_CALENDAR_COMMANDS


def test_calendar_help_lists_remaining_commands_only(tracker):
    result = CliRunner().invoke(tracker["cli"], ["calendar", "--help"])
    assert result.exit_code == 0, result.output
    for name in EXPECTED_CALENDAR_COMMANDS:
        assert name in result.output
    for gone in ("poll", "remind", "install-poll-cron", "export", "unlink"):
        assert f"  {gone}" not in result.output


def test_top_level_add_cron_is_gone_and_calendar_add_cron_writes(tracker):
    from db.manager import DatabaseManager

    assert "add-cron" not in tracker["cli"].commands
    DatabaseManager().add_project(
        project_id="demo", name="demo", path="/nonexistent/demo", status="active"
    )
    result = CliRunner().invoke(
        tracker["cli"], ["calendar", "add-cron", "demo", "0 * * * *", "echo hi"]
    )
    assert result.exit_code == 0, result.output
    with sqlite3.connect(tracker["db_path"]) as conn:
        rows = conn.execute("SELECT project_id, schedule, command FROM cron_jobs").fetchall()
    assert rows == [("demo", "0 * * * *", "echo hi")]


def test_list_create_and_done_still_work(client):
    created = client.post(
        "/api/calendar/events",
        json={"title": "Ship", "event_date": "2030-01-01", "event_time": "09:30"},
    )
    assert created.status_code == 200, created.text
    event_id = created.json()["id"]

    listing = client.get("/api/calendar/events?days=730&include_all=true")
    assert listing.status_code == 200, listing.text
    assert [e["id"] for e in listing.json()["events"]] == [event_id]

    done = client.patch(f"/api/calendar/events/{event_id}/done")
    assert done.status_code == 200, done.text
    assert client.patch("/api/calendar/events/999/done").status_code == 404


def test_cli_done_and_cancel_kept(tracker):
    from db.calendar_manager import CalendarManager

    cm = CalendarManager()
    a = cm.add_event("A", "2030-01-01")
    b = cm.add_event("B", "2030-01-02")
    runner = CliRunner()
    assert runner.invoke(tracker["cli"], ["calendar", "done", str(a)]).exit_code == 0
    assert runner.invoke(tracker["cli"], ["calendar", "cancel", str(b)]).exit_code == 0
    assert cm.get_event(a)["status"] == "done"
    assert cm.get_event(b)["status"] == "cancelled"


# --- date / time validation -------------------------------------------------

@pytest.mark.parametrize("bad", BAD_DATES)
def test_cli_add_rejects_bad_date(tracker, bad):
    result = CliRunner().invoke(tracker["cli"], ["calendar", "add", "X", "--date", bad])
    assert result.exit_code == 2, result.output
    assert "YYYY-MM-DD" in result.output
    assert _event_rows(tracker["db_path"]) == []


@pytest.mark.parametrize("bad", BAD_TIMES)
def test_cli_add_rejects_bad_time(tracker, bad):
    result = CliRunner().invoke(
        tracker["cli"], ["calendar", "add", "X", "--date", "2030-01-01", "--time", bad]
    )
    assert result.exit_code == 2, result.output
    assert "HH:MM" in result.output
    assert _event_rows(tracker["db_path"]) == []


def test_cli_add_accepts_good_date_and_time(tracker):
    result = CliRunner().invoke(
        tracker["cli"],
        ["calendar", "add", "X", "--date", "2028-02-29", "--time", "23:59"],
    )
    assert result.exit_code == 0, result.output
    assert _event_rows(tracker["db_path"]) == [("2028-02-29", "23:59")]


@pytest.mark.parametrize(
    "args",
    [["--date", "2026-13-01"], ["--date", "tomorrow"], ["--date", "2026-02-30"],
     ["--time", "25:00"], ["--time", "9am"]],
)
def test_cli_update_rejects_bad_date_and_time(tracker, args):
    from db.calendar_manager import CalendarManager

    cm = CalendarManager()
    event_id = cm.add_event("A", "2030-01-01", event_time="08:00")
    result = CliRunner().invoke(tracker["cli"], ["calendar", "update", str(event_id), *args])
    assert result.exit_code == 2, result.output
    assert _event_rows(tracker["db_path"]) == [("2030-01-01", "08:00")]


def test_cli_update_accepts_good_date_and_time(tracker):
    from db.calendar_manager import CalendarManager

    cm = CalendarManager()
    event_id = cm.add_event("A", "2030-01-01")
    result = CliRunner().invoke(
        tracker["cli"],
        ["calendar", "update", str(event_id), "--date", "2030-06-30", "--time", "00:00"],
    )
    assert result.exit_code == 0, result.output
    assert _event_rows(tracker["db_path"]) == [("2030-06-30", "00:00")]


@pytest.mark.parametrize("bad", BAD_DATES)
def test_api_create_rejects_bad_date(client, tracker, bad):
    resp = client.post("/api/calendar/events", json={"title": "X", "event_date": bad})
    assert resp.status_code == 422, resp.text
    assert _event_rows(tracker["db_path"]) == []


@pytest.mark.parametrize("bad", BAD_TIMES)
def test_api_create_rejects_bad_time(client, tracker, bad):
    resp = client.post(
        "/api/calendar/events",
        json={"title": "X", "event_date": "2030-01-01", "event_time": bad},
    )
    assert resp.status_code == 422, resp.text
    assert _event_rows(tracker["db_path"]) == []


def test_api_create_accepts_good_date_and_time_and_null_time(client, tracker):
    ok = client.post(
        "/api/calendar/events",
        json={"title": "X", "event_date": "2028-02-29", "event_time": "23:59"},
    )
    assert ok.status_code == 200, ok.text
    none = client.post(
        "/api/calendar/events",
        json={"title": "Y", "event_date": "2030-01-01", "event_time": None},
    )
    assert none.status_code == 200, none.text
    assert sorted(_event_rows(tracker["db_path"])) == [
        ("2028-02-29", "23:59"), ("2030-01-01", None),
    ]


def test_manager_update_rejects_bad_values_for_any_caller(tracker):
    from db.calendar_manager import CalendarManager

    cm = CalendarManager()
    event_id = cm.add_event("A", "2030-01-01")
    with pytest.raises(ValueError):
        cm.update_event(event_id, event_date="2026-02-30")
    with pytest.raises(ValueError):
        cm.update_event(event_id, event_time="9am")
    with pytest.raises(ValueError):
        cm.add_event("B", "tomorrow")


def test_cli_update_empty_time_clears_it(tracker):
    from db.calendar_manager import CalendarManager

    cm = CalendarManager()
    event_id = cm.add_event("A", "2030-01-01", event_time="08:00")
    result = CliRunner().invoke(tracker["cli"], ["calendar", "update", str(event_id), "--time", ""])
    assert result.exit_code == 0, result.output
    assert _event_rows(tracker["db_path"]) == [("2030-01-01", None)]


def test_cli_add_empty_time_means_no_time(tracker):
    result = CliRunner().invoke(
        tracker["cli"], ["calendar", "add", "X", "--date", "2030-01-01", "--time", ""]
    )
    assert result.exit_code == 0, result.output
    assert _event_rows(tracker["db_path"]) == [("2030-01-01", None)]


def test_api_create_empty_time_means_no_time(client, tracker):
    resp = client.post("/api/calendar/events", json={"title": "X", "event_date": "2030-01-01", "event_time": ""})
    assert resp.status_code == 200, resp.text
    assert _event_rows(tracker["db_path"]) == [("2030-01-01", None)]
