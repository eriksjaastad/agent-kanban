"""pt morning: one status report for the Architect's morning start (#8960)."""

import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import PurePath

import click
from db.calendar_manager import CalendarManager
from db.manager import DatabaseManager

WARP_DB = (
    "~/Library/Group Containers/2BBY89MBSN.dev.warp/Library/Application Support/"
    "dev.warp.Warp-Stable/warp.sqlite"
)
PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
CALENDAR_DAYS = 7

# An ERIK: card is "tab" (Erik must be hands-on: a login, a review, a click) when its
# title or latest note contains one of these as a whole word or phrase; otherwise
# "relay" (a decision or fact the Architect can pass on). List each inflection wanted.
TAB_KEYWORDS = (
    "login", "log in", "logins", "sign in", "sign-in", "authorize", "authenticate",
    "password", "2fa", "review", "browser", "click", "install", "hands-on", "in the tab",
)
_TAB_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in TAB_KEYWORDS) + r")\b", re.IGNORECASE
)


def _warp_cwds():
    """(cwds, error): the working directories of Warp's open panes, read-only."""
    path = os.path.expanduser(os.environ.get("PT_WARP_DB") or WARP_DB)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT cwd FROM terminal_panes").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], f"warp db unreadable ({path}): {exc}"
    return [r[0] for r in rows if r[0]], None


def _project_for(cwd, paths):
    """The project whose path is cwd or its nearest ancestor, else None."""
    p = PurePath(os.path.normpath(cwd))
    for candidate in (p, *p.parents):
        if str(candidate) in paths:
            return paths[str(candidate)]
    return None


def _latest_note(notes):
    """The last non-empty paragraph of a card's notes."""
    parts = [p.strip() for p in (notes or "").split("\n\n") if p.strip()]
    return parts[-1] if parts else ""


def _card(task):
    return {
        "id": task["id"],
        "project": task["project_id"],
        "title": task.get("title") or task.get("text") or "",
        "priority": task.get("priority"),
    }


def _title(task):
    return task.get("title") or task.get("text") or ""


def _agent_tasks(tasks):
    """Non-ERIK cards."""
    return [t for t in tasks if not _title(t).startswith("ERIK:")]


def work_can_move(tasks, is_blocked):
    """True if any non-ERIK card is In Progress or Review, or is To Do
    and not blocked. A Mini-held card counts: it can still move, just not from the MacBook."""
    return any(
        t["status"] in ("In Progress", "Review")
        or (t["status"] == "To Do" and not is_blocked(t["id"]))
        for t in _agent_tasks(tasks)
    )


def build_report(*, projects, tasks, checkouts, events, cwds, display_ids, is_blocked, today,
                 warp_error=None):
    """Pure gathering over plain data. ``is_blocked(task_pk)`` is the board's own rule;
    ``display_ids(pks)`` maps task PKs to the ids people see.

    ``all_blocked`` depends on the board only: ERIK: cards exist and not work_can_move().
    ``tabs_idle`` is set when ready cards exist only outside open tabs, and never when the
    tabs are unknown. ``warp_error`` (tabs unreadable) is reported in ``errors``."""
    paths = {os.path.normpath(p["path"]): p["id"] for p in projects if p["path"]}
    paths.pop(os.path.normpath(os.path.expanduser("~/projects")), None)
    open_tabs, unmatched = [], []
    for cwd in cwds:
        pid = _project_for(cwd, paths)
        bucket, name = (open_tabs, pid) if pid else (unmatched, cwd)
        if name not in bucket:
            bucket.append(name)

    title = _title
    ready_cards = [
        t for t in _agent_tasks(tasks)
        if t["status"] == "To Do"
        and (checkouts.get(t["project_id"]) or {}).get("machine") != "mini"
        and not is_blocked(t["id"])
    ]
    ready = [_card(t) for t in ready_cards if t["project_id"] in open_tabs]
    elsewhere = {}
    for t in ready_cards:
        if t["project_id"] not in open_tabs:
            elsewhere.setdefault(t["project_id"], []).append(_card(t))

    erik_tasks = sorted(
        (t for t in tasks if title(t).startswith("ERIK:") and t["status"] not in ("Done", "Cancelled")),
        key=lambda t: (PRIORITY_ORDER.get(t.get("priority"), 4), t["created_at"]),
    )
    erik = []
    for t in erik_tasks:
        ask = _latest_note(t.get("notes"))
        how = "tab" if _TAB_RE.search(f"{title(t)}\n{ask}") else "relay"
        erik.append({**_card(t), "how": how, "ask": ask})

    horizon = (today + timedelta(days=CALENDAR_DAYS)).isoformat()
    calendar = [
        {"id": e["id"], "date": e["event_date"], "time": e.get("event_time"), "title": e["title"],
         "type": e["event_type"], "project": e.get("project_id"),
         "overdue": e["event_date"] < today.isoformat()}
        for e in events if e["status"] == "active" and e["event_date"] <= horizon
    ]
    cards = ready + erik + [c for group in elsewhere.values() for c in group]
    shown = display_ids([c["id"] for c in cards])
    for c in cards:
        c["id"] = shown.get(c["id"], c["id"])
    all_blocked = None
    if erik and not work_can_move(tasks, is_blocked):
        all_blocked = "nothing moves until Erik does these: " + ", ".join(
            f"#{c['id']}" for c in erik)
    tabs_idle = None
    if not ready and elsewhere and not warp_error:
        ids = ", ".join(f"#{c['id']}" for group in elsewhere.values() for c in group)
        tabs_idle = f"no open tab has a ready card; ready elsewhere: {ids}"
    return {
        "open_tabs": open_tabs, "unmatched_tabs": unmatched, "ready": ready, "erik": erik,
        "elsewhere": elsewhere, "calendar": calendar, "all_blocked": all_blocked,
        "tabs_idle": tabs_idle,
        "errors": [warp_error] if warp_error else [],
    }


def _card_line(c):
    return f"  #{c['id']} [{c['project']}] {c['priority'] or '-'} | {c['title']}"


def render_text(r):
    out = ["Open tabs: " + (", ".join(r["open_tabs"]) or "none")]
    if r["unmatched_tabs"]:
        out.append("Unmatched tabs: " + ", ".join(r["unmatched_tabs"]))
    out += [f"ERROR: {e}" for e in r["errors"]]
    out += ["", "Ready now (open tabs):"] + ([_card_line(c) for c in r["ready"]] or ["  none"])
    out += ["", "Needs Erik:"]
    out += [f"{_card_line(c)} ({c['how']})" for c in r["erik"]] or ["  none"]
    out += ["", "Ready elsewhere (no open tab):"]
    for pid, cards in r["elsewhere"].items():
        out += [f"  {pid}: " + "; ".join(f"#{c['id']} {c['title']}" for c in cards)]
    if not r["elsewhere"]:
        out.append("  none")
    out += ["", "Calendar:"]
    out += [f"  {'OVERDUE ' if e['overdue'] else ''}{e['date']} {e['type']} | {e['title']}"
            for e in r["calendar"]] or ["  none"]
    for line in (r["tabs_idle"], r["all_blocked"]):
        if line:
            out += ["", line]
    return "\n".join(out)


@click.command(name="morning")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def morning_cmd(json_output):
    """One report for the morning start: tabs, ready cards, what needs Erik, calendar.

    Open tabs come from Warp's own database (read-only; PT_WARP_DB overrides the
    path), each mapped to the project whose path is the tab's directory or its
    nearest parent. "ready" is To Do, not ERIK:, not blocked, not checked out to
    the Mini. ERIK: cards are "relay" or "tab" by the keywords in TAB_KEYWORDS.

    Two summary lines depend only on the board, never on tab state: all_blocked
    ("nothing moves until Erik does these") when ERIK: cards exist and no non-ERIK card
    is In Progress, in Review, or To Do and unblocked (a Mini-held card
    counts); tabs_idle when ready cards exist only outside the open tabs.
    """
    db = DatabaseManager()
    cwds, warp_error = _warp_cwds()
    report = build_report(
        projects=db.get_all_projects(),
        tasks=db.get_tasks(),
        checkouts=db.get_open_checkouts(),
        events=CalendarManager(db.db_path).get_events(include_all=True),
        cwds=cwds,
        display_ids=db.get_task_display_id_map,
        is_blocked=lambda pk: db.is_blocked(pk)[0],
        today=datetime.now(UTC).date(),
        warp_error=warp_error,
    )
    click.echo(json.dumps(report, indent=2) if json_output else render_text(report))
