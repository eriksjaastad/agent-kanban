"""Focus blocks (#8786): an end time on calendar events and `pt calendar now`."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import example, given
from hypothesis import strategies as st

from commands import calendar as calendar_commands
from db.backend_calendar_manager import validate_event_date, validate_event_time
from db.calendar_manager import CalendarManager
from db.migration_runner import discover_migrations
from pt import cli

MONDAY = datetime(2026, 11, 2, 10, 30)  # a Monday


@pytest.fixture
def cm(isolated_database):
    return CalendarManager()


def _block(cm, title, start, end, date="2026-11-02", **extra):
    return cm.add_event(title, date, event_time=start, end_time=end, **extra)


@pytest.mark.parametrize("start, end, message", [
    (None, "10:00", "needs an event_time"),
    ("10:00", "10:00", "must be after"),
    ("10:00", "09:00", "must be after"),
    ("10:00", "25:00", "24-hour"),
])
def test_an_end_needs_a_start_before_it(cm, start, end, message):
    with pytest.raises(ValueError, match=message):
        _block(cm, "x", start, end)


def test_update_checks_the_end_against_the_stored_time_and_can_clear_it(cm):
    event_id = _block(cm, "x", "09:00", "10:00")
    with pytest.raises(ValueError, match="must be after"):
        cm.update_event(event_id, event_time="11:00")
    with pytest.raises(ValueError, match="needs an event_time"):
        cm.update_event(event_id, event_time="")
    assert cm.update_event(event_id, end_time="")
    assert cm.get_event(event_id)["end_time"] is None
    assert cm.update_event(event_id, end_time="12:00") and cm.get_event(event_id)["end_time"] == "12:00"
    assert cm.update_event(999, end_time="12:00") is False


def test_blocks_follow_recurrence_overlap_and_status(cm):
    _block(cm, "daily", "09:00", "12:00", date="2026-10-01", recurrence="daily")
    _block(cm, "later start", "10:00", "11:00")
    _block(cm, "tuesdays", "10:15", "10:45", date="2026-10-27", recurrence="weekly")
    done = _block(cm, "done", "10:20", "10:40")
    cm.mark_done(done)
    _block(cm, "untimed end", None, None)
    found = cm.blocks_at(MONDAY)
    assert found["current"]["title"] == "later start"  # the overlapping block that started last
    assert found["next"]["title"] == "daily" and found["next"]["start"].date().isoformat() == "2026-11-03"


def test_next_finds_a_monthly_block_weeks_ahead_and_nothing_when_empty(cm):
    assert cm.blocks_at(MONDAY) == {"current": None, "next": None}
    _block(cm, "monthly", "08:00", "09:00", date="2026-10-01", recurrence="monthly")
    found = cm.blocks_at(MONDAY)
    assert found["current"] is None and found["next"]["start"].date().isoformat() == "2026-12-01"


@pytest.mark.parametrize("start_date, recurrence, today, expected", [
    ("2026-01-31", "monthly", "2026-02-01", "2026-03-31"),  # February has no 31st
    ("2026-01-30", "monthly", "2026-02-01", "2026-03-30"),
    ("2027-01-15", None, "2026-11-02", "2027-01-15"),       # a one-off months away
    ("2026-01-05", "weekly", "2026-11-02", "2026-11-02"),   # controls: weekly and daily
    ("2026-01-05", "daily", "2026-11-02", "2026-11-02"),
])
def test_next_is_the_next_real_occurrence_however_far(cm, start_date, recurrence, today, expected):
    _block(cm, "b", "11:00", "12:00", date=start_date, recurrence=recurrence)
    found = cm.blocks_at(datetime.fromisoformat(f"{today}T10:00"))
    assert found["next"]["start"].date().isoformat() == expected


def test_now_json_is_the_agreed_shape(cm, monkeypatch):
    _block(cm, "Deep work", "10:00", "11:00")
    monkeypatch.setattr(calendar_commands, "_local_now", lambda: MONDAY)
    result = CliRunner().invoke(cli, ["calendar", "now", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"schema_version", "current", "next"} and payload["next"] is None
    assert payload["schema_version"] == "pt.calendar.now.v1"
    current = payload["current"]
    assert set(current) == {"id", "title", "project", "start", "end"}
    assert (current["title"], current["project"]) == ("Deep work", None)
    assert (current["start"], current["end"]) == ("2026-11-02T10:00:00", "2026-11-02T11:00:00")


def test_now_failure_is_stderr_and_a_nonzero_exit(cm, monkeypatch):
    def broken():
        raise OSError("disk gone")

    monkeypatch.setattr(calendar_commands, "_get_calendar_manager", broken)
    result = CliRunner().invoke(cli, ["calendar", "now", "--json"])
    assert result.exit_code != 0
    assert result.stdout == "" and "disk gone" in result.stderr


@pytest.mark.parametrize("args", [
    ["add", "x", "--date", "2026-11-02", "--end", "10:00"],
    ["add", "x", "--date", "2026-11-02", "--time", "10:00", "--end", "09:00"],
])
def test_a_rejected_add_exits_2_on_stderr(cm, args):
    result = CliRunner().invoke(cli, ["calendar", *args])
    assert (result.exit_code, result.stdout) == (2, "")
    assert "must be after" in result.stderr or "needs an event_time" in result.stderr
    ok = CliRunner().invoke(cli, ["calendar", "add", "x", "--date", "2026-11-02", "--time", "09:00", "--end", "10:00"])
    assert ok.exit_code == 0 and "Created event" in ok.stdout


def test_a_rejected_update_exits_2_on_stderr_and_a_valid_one_clears(cm):
    event_id = _block(cm, "x", "09:00", "10:00")
    result = CliRunner().invoke(cli, ["calendar", "update", str(event_id), "--time", ""])
    assert (result.exit_code, result.stdout) == (2, "") and "needs an event_time" in result.stderr
    cleared = CliRunner().invoke(cli, ["calendar", "update", str(event_id), "--end", ""])
    assert cleared.exit_code == 0 and cm.get_event(event_id)["end_time"] is None


def test_migration_025_adds_end_time_to_an_existing_table_and_keeps_its_rows(tmp_path: Path):
    [m025] = [m for m in discover_migrations(Path(__file__).parent.parent / "scripts" / "db" / "migrations") if m.version == 25]
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE calendar_events (id INTEGER PRIMARY KEY, title TEXT NOT NULL, event_time TEXT)")
    conn.execute("INSERT INTO calendar_events VALUES (1, 'kept', '09:00')")
    m025.up(conn)
    m025.up(conn)  # idempotent
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(calendar_events)")}
    assert columns["end_time"][2:5] == ("TEXT", 0, None)
    assert conn.execute("SELECT id, title, event_time, end_time FROM calendar_events").fetchall() == [(1, "kept", "09:00", None)]


@given(st.text(max_size=12))
@example("1٠:00")  # Arabic-Indic zero
@example("202６-11-02")  # full-width six
def test_accepted_dates_and_times_are_ascii_and_round_trip(text):
    """Block comparison and ISO parsing assume canonical ASCII, so nothing else gets in."""
    for validate, fmt in ((validate_event_time, "%H:%M"), (validate_event_date, "%Y-%m-%d")):
        try:
            validate(text)
        except ValueError:
            continue
        assert text.isascii() and datetime.strptime(text, fmt).strftime(fmt) == text


@pytest.mark.parametrize("column, stored", [("event_time", "0٩:00"), ("event_date", "202６-11-02")])
def test_an_old_non_ascii_value_cannot_become_a_block(cm, column, stored):
    event_id = cm.add_event("old", "2026-11-02", event_time="09:00")
    with cm._conn() as conn:  # as an earlier, looser validator could have stored it
        conn.execute(f"UPDATE calendar_events SET {column} = ? WHERE id = ?", (stored, event_id))
        conn.commit()
    with pytest.raises(ValueError):
        cm.update_event(event_id, end_time="12:00")
    assert cm.get_event(event_id)["end_time"] is None


def test_now_on_an_unreadable_database_prints_nothing_to_stdout(isolated_database):
    """The real start-up path, in a subprocess: the freshness check skips itself under pytest."""
    isolated_database.write_bytes(b"not a sqlite database")
    env = {k: v for k, v in os.environ.items() if k not in ("PT_ALLOW_FRESH_DB", "PT_TEST_MODE")}
    script = Path(__file__).resolve().parent.parent / "scripts" / "pt.py"
    result = subprocess.run([sys.executable, str(script), "calendar", "now", "--json"],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0 and result.stdout == "", result.stdout
    assert "Could not check database freshness" in result.stderr


def test_today_lists_todays_events_recurring_included_untimed_first(cm):
    from datetime import date

    from fastapi.testclient import TestClient

    from dashboard.app import app

    today = date.today()
    _block(cm, "focus", "09:00", "11:00", date="2026-01-01", recurrence="daily")
    cm.add_event("all day thing", today.isoformat())
    cm.add_event("later", today.isoformat(), event_time="15:00")
    done = cm.add_event("done", today.isoformat(), event_time="08:00")
    cm.mark_done(done)
    cm.add_event("tomorrow", (today + timedelta(days=1)).isoformat())
    body = TestClient(app).get("/api/calendar/today").json()
    assert body["date"] == today.isoformat()
    assert [e["title"] for e in body["events"]] == ["all day thing", "focus", "later"]
    assert body["unreadable"] == []


@pytest.mark.parametrize("column, stored", [("event_date", "202６-11-02"), ("event_time", "0٩:00")])
def test_an_unreadable_stored_event_is_reported_not_fatal(cm, column, stored):
    from datetime import date

    today = date.today()
    bad = cm.add_event("bad", today.isoformat(), event_time="09:00")
    with cm._conn() as conn:  # as a looser, older validator could have stored it
        conn.execute(f"UPDATE calendar_events SET {column} = ? WHERE id = ?", (stored, bad))
        conn.commit()
    _block(cm, "good", "10:00", "11:00", date="2026-01-01", recurrence="daily")
    found = cm.events_on(today)
    assert [e["title"] for e in found["events"]] == ["good"]
    assert [e["title"] for e in found["unreadable"]] == ["bad"]
