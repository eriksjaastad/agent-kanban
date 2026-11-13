"""pt morning (#8960): tab mapping, ready/ERIK/elsewhere sorting, calendar, honest errors."""

import json
import sqlite3
from datetime import date

import pytest
from click.testing import CliRunner
from commands.morning import build_report, morning_cmd, render_text
from db.calendar_manager import CalendarManager

TODAY = date(2026, 11, 12)
AC = "\n- [ ] it works"


@pytest.fixture
def board(db):
    for pid in ("alpha", "beta", "gamma", "mini-proj"):
        db.add_project(pid, pid, f"/p/{pid}", "active")
    db.checkout_project("mini-proj", "mini", "tester")
    add = lambda text, pid, **kw: db.add_task(text, pid, notes=AC, **kw)["id"]
    cards = {
        "ready": add("Ready card", "alpha", status="To Do"),
        "elsewhere": add("Elsewhere card", "gamma", status="To Do"),
        "miniown": add("On the mini", "mini-proj", status="To Do"),
        "backlog": add("Not started", "alpha", status="Backlog"),
        "erik_low": add("ERIK: pick a name", "beta", status="Backlog", priority="Low"),
        "erik_crit": add("ERIK: log in to the vendor", "beta", status="Backlog", priority="Critical"),
        "erik_done": add("ERIK: old ask", "beta", status="Done"),
    }
    cards["blocker"] = add("Unfinished prerequisite", "alpha", status="In Progress")
    cards["blocked"] = add("Waits on prerequisite", "alpha", status="To Do", blocked_by=[cards["blocker"]])
    cards["unblocked"] = add("Prerequisite is done", "alpha", status="To Do", blocked_by=[cards["erik_done"]])
    return cards


def report(db, cwds, events=(), warp_error=None):
    return build_report(
        projects=db.get_all_projects(), tasks=db.get_tasks(), checkouts=db.get_open_checkouts(),
        events=list(events), cwds=cwds, display_ids=db.get_task_display_id_map,
        is_blocked=lambda pk: db.is_blocked(pk)[0], today=TODAY, warp_error=warp_error,
    )


def titles(cards):
    return [c["title"] for c in cards]


def test_cwd_maps_to_project_subdir_and_unmatched(db, board):
    r = report(db, ["/p/alpha", "/p/alpha/.worktrees/9", "/p/beta/src", "/p", "/elsewhere"])
    assert r["open_tabs"] == ["alpha", "beta"]
    assert r["unmatched_tabs"] == ["/p", "/elsewhere"]


def test_ready_excludes_blocked_erik_mini_and_unstarted(db, board):
    r = report(db, ["/p/alpha", "/p/mini-proj"])
    assert sorted(titles(r["ready"])) == ["Prerequisite is done", "Ready card"]
    assert titles(r["elsewhere"]["gamma"]) == ["Elsewhere card"]
    assert "mini-proj" not in r["elsewhere"]


def test_erik_ordered_by_priority_with_relay_or_tab(db, board):
    r = report(db, ["/p/alpha"])
    assert [(c["title"], c["how"]) for c in r["erik"]] == [
        ("ERIK: log in to the vendor", "tab"),
        ("ERIK: pick a name", "relay"),
    ]


@pytest.mark.parametrize("cwds", [[], ["/elsewhere"]])
def test_ready_elsewhere_means_tabs_idle_not_all_blocked(db, board, cwds):
    r = report(db, cwds)
    assert r["all_blocked"] is None
    assert r["tabs_idle"].startswith("no open tab has a ready card; ready elsewhere: #")
    assert "Open tabs: none" in render_text(r)


def test_unreadable_tabs_are_not_idle_tabs(db, board):
    r = report(db, [], warp_error="warp db unreadable")
    assert r["all_blocked"] is None and r["tabs_idle"] is None


def test_open_tab_with_ready_card_sets_neither(db, board):
    r = report(db, ["/p/alpha"])
    assert r["all_blocked"] is None and r["tabs_idle"] is None


def only_erik_cards(db, board, keep=None):
    """Park every non-ERIK card in Backlog except ``keep`` (card key -> status)."""
    for key in ("ready", "elsewhere", "miniown", "blocker", "blocked", "unblocked"):
        db.update_task(board[key], status=(keep or {}).get(key, "Backlog"))


@pytest.mark.parametrize("cwds,warp_error", [([], None), (["/p/beta"], None), ([], "warp db unreadable")])
def test_all_blocked_when_no_work_can_move(db, board, cwds, warp_error):
    only_erik_cards(db, board)
    r = report(db, cwds, warp_error=warp_error)
    ids = ", ".join(f"#{c['id']}" for c in r["erik"])
    assert r["all_blocked"] == f"nothing moves until Erik does these: {ids}"
    assert r["tabs_idle"] is None


@pytest.mark.parametrize("keep", [
    {"blocker": "In Progress"},
    {"blocker": "Review"},
    {"miniown": "To Do"},
    {"blocker": "In Progress", "blocked": "To Do"},
])
def test_work_that_can_still_move_clears_all_blocked(db, board, keep):
    only_erik_cards(db, board, keep)
    assert report(db, [])["all_blocked"] is None


def test_calendar_upcoming_and_overdue(db):
    cm = CalendarManager(db.db_path)
    cm.add_event("overdue", "2026-11-10")
    cm.add_event("soon", "2026-11-19")
    cm.add_event("far", "2026-11-20")
    done = cm.add_event("done", "2026-11-01")
    cm.mark_done(done)
    r = report(db, [], cm.get_events(include_all=True))
    assert [(e["title"], e["overdue"]) for e in r["calendar"]] == [
        ("overdue", True), ("soon", False)]


def test_missing_warp_db_is_reported_not_hidden(db, board, monkeypatch, tmp_path):
    monkeypatch.setenv("PT_WARP_DB", str(tmp_path / "nope.sqlite"))
    out = CliRunner().invoke(morning_cmd, ["--json"])
    data = json.loads(out.output)
    assert out.exit_code == 0 and data["open_tabs"] == []
    assert "warp db unreadable" in data["errors"][0]
    assert data["erik"] and data["all_blocked"] is None


def test_cli_reads_warp_db_read_only(db, board, monkeypatch, tmp_path):
    warp = tmp_path / "warp.sqlite"
    conn = sqlite3.connect(warp)
    conn.execute("CREATE TABLE terminal_panes (cwd TEXT)")
    conn.execute("INSERT INTO terminal_panes VALUES ('/p/alpha/sub')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("PT_WARP_DB", str(warp))
    out = CliRunner().invoke(morning_cmd, [])
    assert out.exit_code == 0
    assert "Open tabs: alpha" in out.output and "Ready card" in out.output


@pytest.mark.parametrize("title,how", [
    ("running total", "relay"),
    ("author the plan", "relay"),
    ("pick a name", "relay"),
    ("log in to Vercel", "tab"),
    ("review PR #12", "tab"),
    ("Authorize the app", "tab"),
    ("do it hands-on", "tab"),
])
def test_relay_or_tab_by_whole_words(db, title, how):
    db.add_project("zeta", "zeta", "/p/zeta", "active")
    db.add_task("ERIK: " + title, "zeta", status="Backlog", notes=AC)
    assert report(db, [])["erik"][0]["how"] == how


def test_equal_priority_orders_oldest_first(db):
    db.add_project("zeta", "zeta", "/p/zeta", "active")
    new = db.add_task("ERIK: newer", "zeta", priority="High", notes=AC)["id"]
    old = db.add_task("ERIK: older", "zeta", priority="High", notes=AC)["id"]
    db.seed(new, created_at="2026-02-01T00:00:00")
    db.seed(old, created_at="2026-01-01T00:00:00")
    assert titles(report(db, [])["erik"]) == ["ERIK: older", "ERIK: newer"]


def test_text_has_every_section(db, board):
    cm = CalendarManager(db.db_path)
    cm.add_event("late thing", "2026-11-10")
    text = render_text(report(db, ["/p/beta"], cm.get_events(include_all=True)))
    assert "Ready elsewhere" in text and "gamma: " in text and "Elsewhere card" in text
    assert "OVERDUE 2026-11-10 reminder | late thing" in text
    assert "no open tab has a ready card" in text and "nothing moves" not in text


def test_json_ready_card_shape(db, board):
    r = report(db, ["/p/alpha"])
    card = next(c for c in r["ready"] if c["title"] == "Ready card")
    assert set(card) == {"id", "project", "title", "priority"}
    assert card["project"] == "alpha" and isinstance(json.dumps(r), str)
