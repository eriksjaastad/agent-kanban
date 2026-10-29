"""Project checkouts: `pt project checkout|checkin`, the task listings and /api/projects (#8339)."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from hypothesis import example, given
from hypothesis import strategies as st
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from pt import cli  # noqa: E402
from dashboard.app import app  # noqa: E402

migration = importlib.import_module("db.migrations.024_add_checkout_task_id")

ENV = {"PT_NO_BANNER": "1", "PT_SUPPRESS_MIGRATION_WARNING": "1"}


def _pt(*args: str):
    return CliRunner().invoke(cli, list(args), env=ENV)


@pytest.fixture
def holoapp(db):
    db.add_project(project_id="holoapp", name="holoapp", path="virtual://holoapp", status="active")
    return db


def test_second_checkout_is_refused_naming_the_holder(holoapp) -> None:
    first = _pt("project", "checkout", "holoapp", "--machine", "mini", "--holder", "Qwen on Mini")
    assert first.exit_code == 0, first.output
    assert "CHECKED OUT — Mac Mini (Qwen on Mini" in first.output

    second = _pt("project", "checkout", "holoapp", "--machine", "laptop", "--holder", "Claude")
    assert second.exit_code != 0
    assert "already checked out to Mac Mini by Qwen on Mini" in second.output
    assert holoapp.get_open_checkouts()["holoapp"]["holder"] == "Qwen on Mini"


def test_checkout_of_a_missing_project_inserts_nothing(holoapp) -> None:
    # The project check is part of the INSERT, so a project deleted just
    # before it leaves no orphan checkout behind.
    with pytest.raises(ValueError, match="no project 'gone'"):
        holoapp.checkout_project("gone", "mini", "Qwen on Mini")
    with holoapp._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_checkouts").fetchone()[0] == 0


def test_checkin_records_who_and_frees_the_project(holoapp) -> None:
    holoapp.checkout_project("holoapp", "mini", "Qwen on Mini")
    result = _pt("project", "checkin", "holoapp", "--by", "Claude on MacBook")
    assert result.exit_code == 0, result.output
    assert holoapp.get_open_checkouts() == {}
    with holoapp._get_conn() as conn:
        row = conn.execute("SELECT checked_in_by, checked_in_at FROM project_checkouts").fetchone()
    assert row["checked_in_by"] == "Claude on MacBook" and row["checked_in_at"]
    # Nothing is out now, so a second check-in is an error, and a new checkout works.
    assert _pt("project", "checkin", "holoapp", "--by", "x").exit_code != 0
    assert holoapp.checkout_project("holoapp", "laptop", "Claude")["machine"] == "laptop"


def test_checkin_with_checkout_id_is_atomic(holoapp) -> None:
    checkout = holoapp.checkout_project("holoapp", "mini", "Qwen on Mini")
    refused = _pt(
        "project", "checkin", "holoapp", "--by", "Claude on MacBook",
        "--checkout-id", str(checkout["id"] + 1),
    )
    assert refused.exit_code != 0
    assert holoapp.get_open_checkouts()["holoapp"]["id"] == checkout["id"]

    accepted = _pt(
        "project", "checkin", "holoapp", "--by", "Claude on MacBook",
        "--checkout-id", str(checkout["id"]),
    )
    assert accepted.exit_code == 0, accepted.output
    replacement = holoapp.checkout_project("holoapp", "laptop", "Claude")
    assert replacement["id"] != checkout["id"]
    stale = _pt(
        "project", "checkin", "holoapp", "--by", "stale cleanup",
        "--checkout-id", str(checkout["id"]),
    )
    assert stale.exit_code != 0
    assert holoapp.get_open_checkouts()["holoapp"]["id"] == replacement["id"]

    overflow = _pt(
        "project", "checkin", "holoapp", "--by", "tester",
        "--checkout-id", str(2**63),
    )
    assert overflow.exit_code == 2
    assert "not in the range" in overflow.output


def test_listings_and_api_show_the_checkout(holoapp) -> None:
    holoapp.add_task(text="Wire the tab bar", project_id="holoapp", status="To Do")
    holoapp.checkout_project("holoapp", "mini", "Qwen on Mini")

    scoped = _pt("tasks", "-p", "holoapp")
    assert scoped.exit_code == 0, scoped.output
    assert "CHECKED OUT — Mac Mini" in scoped.output
    everything = _pt("tasks")
    assert "[OUT: Mac Mini] | holoapp |" in everything.output
    # Board cells wrap at 20 columns, so check the start of the marker.
    assert "[OUT:" in _pt("tasks", "--board").output
    assert "CHECKED OUT — Mac Mini" in _pt("status", "holoapp").output
    assert "holoapp | active | - | 0% | ! | CHECKED OUT — Mac Mini" in _pt("list").output

    [project] = [p for p in TestClient(app).get("/api/projects").json()["projects"] if p["id"] == "holoapp"]
    assert (project["checkout"]["machine_label"], project["checkout"]["holder"]) == ("Mac Mini", "Qwen on Mini")


def test_before_migration_023_listings_work_and_checkout_says_to_migrate(holoapp) -> None:
    # The per-test database copy, not the live one.
    with sqlite3.connect(os.environ["PT_DB_PATH"]) as conn:
        conn.execute("DROP TABLE project_checkouts")
    assert _pt("tasks", "-p", "holoapp").exit_code == 0
    response = TestClient(app).get("/api/projects")
    assert response.status_code == 200
    assert all(p["checkout"] is None for p in response.json()["projects"])
    refused = _pt("project", "checkout", "holoapp", "--machine", "mini", "--holder", "Qwen")
    assert refused.exit_code != 0 and "run `pt db migrate`" in refused.output


@pytest.fixture
def arc(holoapp):
    """holoapp is out to the Mini on purpose (owned); arc gets checked out for a card."""
    holoapp.add_project(project_id="arc", name="arc", path="virtual://arc", status="active")
    holoapp.checkout_project("holoapp", "mini", "Mac Mini floor manager")
    return holoapp


def _backdate(days: int, task_id=None) -> None:
    """Move every checkout, and the card if given, ``days`` into the past."""
    with sqlite3.connect(os.environ["PT_DB_PATH"]) as conn:
        conn.execute("UPDATE project_checkouts SET checked_out_at = ?",
                     ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),))
        if task_id is not None:
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?",
                         ((datetime.now() - timedelta(days=days)).isoformat(), task_id))


def test_done_on_its_card_reminds_to_check_in_and_an_owned_checkout_never_does(arc) -> None:
    # The card lives in holoapp but the checkout is arc's: the reminder follows the card.
    card = arc.add_task(text="Port the monitor", project_id="holoapp", status="Review")
    other = arc.add_task(text="Unrelated", project_id="holoapp", status="Review")
    taken = _pt("project", "checkout", "arc", "--machine", "mini", "--holder", "Herald", "--card", str(card["id"]))
    assert taken.exit_code == 0, taken.output

    assert "Check in" not in _pt("tasks", "done", str(other["id"])).output
    done = _pt("tasks", "done", str(card["id"]))
    assert done.exit_code == 0, done.output
    assert "Check in arc: out to Mac Mini (Herald)" in done.output and "its card is Done" in done.output
    assert "Check in holoapp" not in done.output

    # The printed command names its checkout, so once that checkout is replaced it closes nothing.
    first = arc.get_open_checkouts()["arc"]["id"]
    command = f"pt project checkin --checkout-id {first} --by '<you>' -- arc"
    assert command in done.output
    arc.checkin_project("arc", "Herald", first)
    replacement = arc.checkout_project("arc", "laptop", "Claude")
    stale = _pt("project", "checkin", "arc", "--checkout-id", str(first), "--by", "<you>")
    assert stale.exit_code != 0 and arc.get_open_checkouts()["arc"]["id"] == replacement["id"]


def test_pt_tasks_reminds_when_a_card_checkout_goes_idle(arc) -> None:
    card = arc.add_task(text="Port the monitor", project_id="arc", status="In Progress")
    arc.checkout_project("arc", "mini", "Herald", task_id=card["id"])
    assert "Check in" not in _pt("tasks").output  # fresh

    _backdate(4, card["id"])
    listing = _pt("tasks").output
    assert "Check in arc" in listing and "no activity for 3+ days" in listing
    assert "Check in holoapp" not in listing  # owned, and just as old
    assert "Check in arc" in _pt("tasks", "--board").output
    assert "Check in" not in _pt("tasks", "-p", "holoapp").output
    # JSON and the API keep migration 023's checkout fields: task_id is not added to them.
    [listed] = [t for t in json.loads(_pt("tasks", "--json").output)["tasks"] if t["project_id"] == "arc"]
    assert set(listed["checkout"]) == {"id", "project_id", "machine", "holder", "note",
                                       "checked_out_at", "checked_in_at", "checked_in_by"}
    assert "task_id" not in json.loads(_pt("tasks", "show", str(card["id"]), "--json").output)["checkout"]
    [project] = [p for p in TestClient(app).get("/api/projects").json()["projects"] if p["id"] == "arc"]
    assert "task_id" not in project["checkout"]

    arc.update_task(card["id"], notes="still on it")  # activity on the card resets the clock
    assert "Check in" not in _pt("tasks").output
    with sqlite3.connect(os.environ["PT_DB_PATH"]) as conn:  # an unreadable time counts as old
        conn.execute("UPDATE tasks SET updated_at = '' WHERE id = ?", (card["id"],))
    assert "no activity for 3+ days" in _pt("tasks").output


def _printed_command(project_id: str, checkout_id: int) -> list[str]:
    """The reminder's command for one due checkout, split the way a shell would."""
    import pt

    due = {"project_id": project_id, "machine": "mini", "holder": "H", "task_id": 1, "reason": "r", "id": checkout_id}
    with pt.console.capture() as captured:
        pt._print_checkin_reminders(None, [due])
    return shlex.split(captured.get().split("Run: ", 1)[1])


HOSTILE_IDS = ["-scratch", "--help", "--", "-", "r&d", ":smile:", "[red]x[/red]", "a b", "it's", "$(whoami)"]


# Printable text only: a terminal line cannot carry control characters (Rich drops them), and a
# command with a garbled project id still closes nothing, because --checkout-id must match it too.
@given(st.text(st.characters(blacklist_categories=("Cc", "Cs")), min_size=1).filter(lambda s: "Run: " not in s))
@example("-scratch")
@example("--help")
@example(":smile:")
def test_the_printed_command_parses_to_the_same_checkin_whatever_the_id(project_id) -> None:
    import pt

    argv = _printed_command(project_id, 7)
    assert argv[:3] == ["pt", "project", "checkin"]
    # The real `pt project checkin` parser, without running it.
    params = pt.project_checkin.make_context("checkin", argv[3:]).params
    assert params == {"project": project_id, "checkout_id": 7, "by": "<you>"}


def test_running_the_printed_command_checks_the_project_in(db) -> None:
    for n, project_id in enumerate(HOSTILE_IDS):
        db.add_project(project_id=project_id, name=project_id, path=f"virtual://hostile-{n}", status="active")
        card = db.add_task(text="t", project_id=project_id, status="Review")
        db.checkout_project(project_id, "mini", "Herald", task_id=card["id"])
        done = _pt("tasks", "done", str(card["id"]))
        argv = shlex.split(done.output.split("Run: ", 1)[1].splitlines()[0])
        replay = _pt(*argv[1:])  # what the shell would run, minus the `pt` launcher
        assert replay.exit_code == 0, (project_id, replay.output)
        assert db.get_open_checkouts() == {}, project_id


def test_a_cancelled_or_deleted_card_is_due_at_once(arc) -> None:
    card = arc.add_task(text="Port the monitor", project_id="arc", status="In Progress")
    arc.checkout_project("arc", "mini", "Herald", task_id=card["id"])
    arc.update_task(card["id"], status="Cancelled")
    assert [due["reason"] for due in arc.checkin_reminders()] == ["its card is Cancelled"]
    with sqlite3.connect(os.environ["PT_DB_PATH"]) as conn:  # the card was deleted (SAFE_MODE blocks delete_task)
        conn.execute("UPDATE project_checkouts SET task_id = 2 * ? WHERE task_id = ?", (card["id"], card["id"]))
    assert [due["reason"] for due in arc.checkin_reminders()] == ["its card no longer exists"]


def test_done_still_completes_when_the_reminder_check_fails(arc, monkeypatch) -> None:
    import pt

    card = arc.add_task(text="Port the monitor", project_id="arc", status="Review")

    def broken(self, project_id=None):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(pt.DatabaseManager, "checkin_reminders", broken)
    done = _pt("tasks", "done", str(card["id"]))
    assert done.exit_code == 0, done.output
    assert "Could not check whether a checkout waits on" in done.output and "disk I/O error" in done.output
    assert arc.get_task(card["id"])["status"] == "Done"


def test_card_must_exist_and_needs_migration_024(holoapp) -> None:
    refused = _pt("project", "checkout", "holoapp", "--machine", "mini", "--holder", "H", "--card", "999999")
    assert refused.exit_code != 0 and "Card #999999 not found" in refused.output
    with pytest.raises(ValueError, match="no card 999999"):  # the insert checks the card too
        holoapp.checkout_project("holoapp", "mini", "H", task_id=999999)
    for bad in ("0", str(2**63)):
        assert _pt("project", "checkout", "holoapp", "--machine", "mini", "--holder", "H",
                   "--card", bad).exit_code == 2
    assert holoapp.get_open_checkouts() == {}

    card = holoapp.add_task(text="x", project_id="holoapp", status="To Do")
    with sqlite3.connect(os.environ["PT_DB_PATH"]) as conn:
        conn.execute("ALTER TABLE project_checkouts DROP COLUMN task_id")
    with pytest.raises(ValueError, match="migration 024"):
        holoapp.checkout_project("holoapp", "mini", "H", task_id=card["id"])
    holoapp.checkout_project("holoapp", "mini", "H")  # an owned checkout still works
    assert holoapp.checkin_reminders() == [] and _pt("tasks").exit_code == 0

    with sqlite3.connect(os.environ["PT_DB_PATH"]) as conn:
        migration.up(conn)
        migration.up(conn)  # idempotent
        assert "task_id" in {row[1] for row in conn.execute("PRAGMA table_info(project_checkouts)")}
