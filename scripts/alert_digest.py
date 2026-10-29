#!/usr/bin/env python3
"""Portfolio alert digest — Phase 1 (MacBook).

ONE daily email that reports anything broken across the portfolio, so failures
stop rotting silently. Reads the dashboard's live alerts, leaves out the
projects listed in the [digest] section's ignore and mini_ignore, renders an
email sectioned by machine, and sends it via
Resend to the recipient in the local config's [digest] section (see "Local
integrations" in scripts/config.py). Without that section the digest is off: it
logs how to turn it on and exits 0.

Design (locked, see PROGRESS.md):
  - One email, sectioned by machine (MacBook real / Mac Mini pending until Phase 3).
  - Reads http://localhost:8000/api/alerts. The dashboard is a KeepAlive launchd
    job so it's reliably up; if it is NOT reachable after retries, that is itself
    a failure worth an email — we send a degraded notice rather than going silent.
  - Never crashes. Silence must never mask a problem.
  - Secrets from Doppler only (RESEND_API_KEY). No fallbacks. With [digest]
    doppler_project/doppler_config set, the script runs itself again under
    `doppler run` for that project; without them, RESEND_API_KEY must already
    be in the environment.

Run:  python scripts/alert_digest.py [--dry-run]

--dry-run prints the rendered email to stdout and does NOT send.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from config import DIGEST_NOT_CONFIGURED, DigestIntegration, digest_integration, local_config_path
from scheduled_jobs import launchctl_list

# --- Config -----------------------------------------------------------------

ALERTS_URL = os.environ.get("PT_ALERTS_URL", "http://localhost:8000/api/alerts")
TASKS_URL = os.environ.get("PT_TASKS_URL", "http://localhost:8000/api/tasks")
CALENDAR_URL = os.environ.get(
    "PT_CALENDAR_URL", "http://localhost:8000/api/calendar/events?include_all=true"
)
RESEND_URL = "https://api.resend.com/emails"
# api.resend.com sits behind Cloudflare, which 403s Python's default urllib
# User-Agent (error code 1010). A browser-ish UA gets through.
BROWSER_UA = "Mozilla/5.0 (portfolio-alert-digest)"

REPO_ROOT = Path(__file__).resolve().parent.parent
MINI_SCAN_SCRIPT = REPO_ROOT / "scripts" / "mini_scan.py"

# Projects touched within this window count as "active"; older = stale warning.
STALE_DAYS = 60

# Kanban card states. "In motion" cards get listed individually in the email;
# the rest are only counted so a 345-deep backlog doesn't drown the signal.
IN_MOTION_STATES = ("In Progress", "Review")
QUEUED_STATE = "To Do"
BACKLOG_STATE = "Backlog"

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
SEVERITY_DOT = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


# --- Small helpers ----------------------------------------------------------

def log(msg: str) -> None:
    """Print a timestamped line to stderr.

    The launchd job sends stderr to logs/alert_digest.stderr.log, so this is
    the digest's one log; it used to append every line to a second file too.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {msg}", file=sys.stderr)


# --- Data fetch -------------------------------------------------------------

def fetch_alerts() -> list:
    """Fetch alerts from the dashboard with retries.

    Returns a list of alert dicts. Raises RuntimeError only after all retries
    are exhausted — the caller turns that into a degraded email.
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                ALERTS_URL, headers={"User-Agent": BROWSER_UA}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            alerts = payload.get("alerts", [])
            log(f"fetched {len(alerts)} alerts (attempt {attempt})")
            return alerts
        except Exception as exc:  # noqa: BLE001 — resilience is the point
            last_err = exc
            log(f"WARN fetch attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise RuntimeError(f"dashboard unreachable after {MAX_RETRIES} attempts: {last_err}")


def fetch_mini_data(host: str, prefixes: tuple[str, ...]) -> dict | None:
    """Run mini_scan.py on the configured second Mac via SSH-pipe. Returns parsed dict.

    Stateless: the scanner is streamed to the host's stdin and run with its own
    python3 — nothing is installed there; the job label prefixes go as its
    arguments. Returns None if the host is unreachable after retries (caller
    renders an "unreachable" notice, never crashes) or if the scanner is
    missing locally.
    """
    if not MINI_SCAN_SCRIPT.exists():
        log(f"WARN mini scanner missing at {MINI_SCAN_SCRIPT}; skipping Mini section")
        return None

    script = MINI_SCAN_SCRIPT.read_text()
    cmd = [
        "ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
        host, "python3", "-", *map(shlex.quote, prefixes),
    ]
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            proc = subprocess.run(
                cmd, input=script, capture_output=True, text=True, timeout=45
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ssh exit {proc.returncode}: {proc.stderr.strip()[:200]}"
                )
            data = json.loads(proc.stdout)
            log(f"mini fetch ok (attempt {attempt}): {len(data.get('projects', []))} projects")
            return data
        except Exception as exc:  # noqa: BLE001 — resilience is the point
            last_err = exc
            log(f"WARN mini fetch attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    log(f"ERROR mini unreachable after {MAX_RETRIES} attempts: {last_err}")
    return None


def fetch_tasks() -> list | None:
    """Fetch all Kanban tasks from the dashboard. None on failure (graceful)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(TASKS_URL, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            tasks = payload.get("tasks", []) if isinstance(payload, dict) else payload
            if not isinstance(tasks, list):
                raise RuntimeError(f"unexpected /api/tasks shape: {type(tasks).__name__}")
            log(f"fetched {len(tasks)} tasks (attempt {attempt})")
            return tasks
        except Exception as exc:  # noqa: BLE001
            log(f"WARN tasks fetch attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    log("ERROR tasks unreachable after retries")
    return None


# Deadlines (#8137): the calendar's only route to the recipient. Show what is due soon or
# was missed recently; an event missed more than a month ago is stale
# bookkeeping, not this morning's problem, so it stays out of the email.
DEADLINE_WINDOW_DAYS = 14
OVERDUE_LOOKBACK_DAYS = 30
DEADLINE_SUBJECT_DAYS = 7


def fetch_calendar_events() -> list | None:
    """Fetch every calendar event from the dashboard. None on failure (graceful)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(CALENDAR_URL, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            events = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
                raise RuntimeError("unexpected /api/calendar/events shape")
            log(f"fetched {len(events)} calendar events (attempt {attempt})")
            return events
        except Exception as exc:  # noqa: BLE001
            log(f"WARN calendar fetch attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    log("ERROR calendar unreachable after retries")
    return None


FINISHED_STATUSES = ("done", "cancelled")


def _shown(value) -> str:
    """repr of an uninterpretable value, capped so one bad row stays one line."""
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _event_day(raw) -> date:
    """A stored event date: YYYY-MM-DD, or a full ISO datetime. ValueError otherwise."""
    text = str(raw or "")
    if len(text) == 10:
        return date.fromisoformat(text)
    return datetime.fromisoformat(text).date()


def upcoming_deadlines(events: list, today: date) -> list[dict]:
    """Deadline rows: active events due within the window or missed within the lookback.

    The rule: an active event is never silently dropped. Only finished events
    (done, cancelled) and events outside the window are left out; an event
    whose status or date can't be interpreted becomes a row with ``days`` None
    and a ``problem`` saying what was unreadable, so it reaches the email.

    Event dates are stored as local wall-clock dates, so ``today`` must be the
    local date. Each row gains ``days`` (negative = overdue). Unreadable rows
    come first, then soonest first.
    """
    found = []
    for event in events:
        status = event.get("status")
        if status in FINISHED_STATUSES:
            continue
        if status != "active":
            log(f"WARN calendar event {event.get('id')} has an unreadable status {status!r}")
            found.append({**event, "days": None, "problem": f"status unreadable: {_shown(status)}"})
            continue
        raw = event.get("event_date")
        try:
            due = _event_day(raw)
        except ValueError:
            log(f"WARN calendar event {event.get('id')} has an unreadable date {_shown(raw)}")
            found.append({**event, "days": None, "problem": f"date unreadable: {_shown(raw)}"})
            continue
        days = (due - today).days
        if -OVERDUE_LOOKBACK_DAYS <= days <= DEADLINE_WINDOW_DAYS:
            found.append({**event, "days": days})
    return sorted(
        found,
        key=lambda e: (e["days"] is not None, e["days"] or 0, str(e.get("event_time") or "")),
    )


def fetch_scheduled_jobs(prefixes: tuple[str, ...]) -> list | None:
    """Return our launchd jobs with status via `launchctl list`.

    Each item: {"label", "pid" (str or None), "last_exit" (int or None)}.
    None if launchctl can't be run (graceful degrade). launchctl list columns
    are: PID, last-exit-status, Label; a numeric PID means currently running and
    the status is that of the previous run.
    """
    try:
        rows = launchctl_list()
    # governance: allow-silent SF002: None is the documented "no job data" value; _render_job_group prints a no-job-data warning in the digest for it
    except Exception as exc:  # noqa: BLE001
        log(f"WARN scheduled-jobs fetch failed: {exc}")
        return None

    jobs = [r for r in rows if r["label"].startswith(prefixes)]
    log(f"scheduled jobs: {len(jobs)} of ours")
    return jobs


# --- Rendering --------------------------------------------------------------

def _summary_counts(alerts: list) -> dict:
    return {
        "critical": sum(1 for a in alerts if a.get("severity") == "critical"),
        "warning": sum(1 for a in alerts if a.get("severity") == "warning"),
        "info": sum(1 for a in alerts if a.get("severity") == "info"),
    }


def _deadline_subject_parts(deadlines: list | None) -> list[str]:
    if deadlines is None:
        return ["⚠️ calendar unreadable"]
    soon = [e for e in deadlines if e["days"] is None or e["days"] <= DEADLINE_SUBJECT_DAYS]
    if soon:
        n = len(soon)
        return [f"📅 {n} deadline" + ("s" if n != 1 else "") + " due or missed"]
    if deadlines:
        # Listed in the body, so the subject must not claim "All clear".
        n = len(deadlines)
        return [f"📅 {n} upcoming deadline" + ("s" if n != 1 else "")]
    return []


def build_subject(alerts: list, deadlines: list | None = ()) -> str:
    c = _summary_counts(alerts)
    early = _deadline_subject_parts(deadlines)
    if not alerts and not early:
        return "[Project Alerts] ✅ All clear"
    parts = list(early)
    if c["critical"]:
        parts.append(f"🔴 {c['critical']} critical")
    if c["warning"]:
        parts.append(f"🟡 {c['warning']} warning" + ("s" if c["warning"] != 1 else ""))
    if c["info"]:
        parts.append(f"🔵 {c['info']} info")
    return "[Project Alerts] " + ", ".join(parts)


def _render_alert_rows(alerts: list) -> str:
    rows = []
    for a in sorted(
        alerts,
        key=lambda x: (SEVERITY_ORDER.get(x.get("severity"), 3), x.get("project_name", "")),
    ):
        dot = SEVERITY_DOT.get(a.get("severity"), "⚪")
        name = a.get("project_name", "unknown")
        message = a.get("message", "")
        details = a.get("details", "")
        rows.append(
            f'<tr>'
            f'<td style="padding:6px 10px;vertical-align:top;white-space:nowrap;">{dot}</td>'
            f'<td style="padding:6px 10px;vertical-align:top;"><strong>{_esc(name)}</strong><br>'
            f'<span style="color:#333;">{_esc(message)}</span>'
            f'<br><span style="color:#888;font-size:12px;">{_esc(details)}</span></td>'
            f'</tr>'
        )
    return "\n".join(rows)


def _esc(s: str) -> str:
    """Escape user-supplied text for an HTML TEXT NODE.

    Handles `& < >` only. That is sufficient between tags and NOT sufficient
    inside an attribute value, where an unescaped quote closes the attribute
    early: `<a title="{_esc(x)}">` with x = `" onmouseover=alert(1) "` breaks
    out. Every current caller interpolates into element content, but there are
    now a dozen of them and the next person adding a `style=` or `href=` will
    reach for the nearest-looking helper. Use `_esc_attr` there (#6881).
    """
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _esc_attr(s: str) -> str:
    """Escape user-supplied text for an HTML ATTRIBUTE VALUE.

    Everything `_esc` does, plus both quote characters, so the value cannot
    terminate the attribute it sits in.
    """
    return _esc(s).replace('"', "&quot;").replace("'", "&#x27;")


def _days_since(iso: str | None) -> int | None:
    """Whole days between the date in an ISO timestamp and today. None if unparseable."""
    if not iso:
        return None
    try:
        d = datetime.strptime(str(iso)[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - d).days
    except (ValueError, TypeError):  # governance: allow-silent SF002: None is the documented "age unknown" value; the card renderer omits the age for it and _age_human prints "?"
        return None


def _age_human(days: int | None) -> str:
    if days is None:
        return "?"
    if days < 1:
        return "today"
    if days < 30:
        return f"{days}d"
    return f"~{days // 30}mo"


REVIEW_STALE_DAYS = 14  # a card sitting in Review longer than this gets flagged


CARD_TITLE_MAX = 80  # keep the email scannable — many cards store a full prompt


def _card_line(t: dict, *, show_age: bool = False, review: bool = False) -> str:
    """One card row: #short title — project [· age]."""
    did = t.get("display_id") or "?"
    raw = (t.get("title") or t.get("text") or "(untitled)").strip()
    # Collapse whitespace/newlines and truncate so one card = one scannable line.
    raw = " ".join(raw.split())
    if len(raw) > CARD_TITLE_MAX:
        raw = raw[:CARD_TITLE_MAX].rstrip() + "…"
    name = _esc(raw)
    proj = _esc(t.get("project_id") or "?")
    age_html = ""
    if show_age:
        days = _days_since(t.get("updated_at"))
        if review and days is not None and days >= REVIEW_STALE_DAYS:
            age_html = f' <strong style="color:#8a1f1f;">· sitting {_age_human(days)}</strong>'
        elif days is not None:
            age_html = f' <span style="color:#999;">· {_age_human(days)}</span>'
    return (
        f'<div style="padding:2px 0;">'
        f'<span style="color:#888;font-variant-numeric:tabular-nums;">#{_esc(did)}</span> '
        f'{name} <span style="color:#aaa;">— {proj}</span>{age_html}</div>'
    )


def render_cards_section(tasks: list | None) -> str:
    """All open cards, grouped by status. Short numbers; review-age flagged.

    In Progress / Review / To Do are listed in full; Backlog is grouped by
    project with counts (listing 345 lines defeats a scannable daily email — but
    the per-project map still shows where parked/forgotten work lives).
    """
    if tasks is None:
        return (
            '<div style="color:#8a6d1f;background:#fffbe6;border:1px solid #e6d999;'
            'border-radius:6px;padding:10px;">⚠️ Could not read the board this run.</div>'
        )

    in_prog = [t for t in tasks if t.get("status") == "In Progress"]
    review = [t for t in tasks if t.get("status") == "Review"]
    todo = [t for t in tasks if t.get("status") == QUEUED_STATE]
    backlog = [t for t in tasks if t.get("status") == BACKLOG_STATE]

    def _subhead(txt):
        return f'<div style="margin:12px 0 2px;font-weight:600;color:#333;">{txt}</div>'

    html = ""

    # All groups default to alphabetical by project. The red "sitting ~Nmo" flag
    # (not sort order) is what surfaces a stale card.
    def _by_project(t):
        return (t.get("project_id") or "~").lower()

    # In Progress
    html += _subhead(f"🔧 In Progress ({len(in_prog)})")
    if in_prog:
        for t in sorted(in_prog, key=_by_project):
            html += _card_line(t, show_age=True)
    else:
        html += '<div style="color:#888;">—</div>'

    # Review — stale ones flagged in red ("sitting ~2mo").
    html += _subhead(f"👀 Review ({len(review)})")
    if review:
        for t in sorted(review, key=_by_project):
            html += _card_line(t, show_age=True, review=True)
    else:
        html += '<div style="color:#888;">—</div>'

    # To Do — full list, grouped by project, alphabetical.
    html += _subhead(f"📋 To Do ({len(todo)})")
    if todo:
        by_proj = {}
        for t in todo:
            by_proj.setdefault(t.get("project_id") or "?", []).append(t)
        for proj in sorted(by_proj, key=str.lower):
            for t in by_proj[proj]:
                html += _card_line(t)
    else:
        html += '<div style="color:#888;">—</div>'

    # Backlog — one line per project (alphabetical), count each. A list, not a
    # comma block — this is the "where's my parked/forgotten work" map.
    html += _subhead(f"🗄️ Backlog ({len(backlog)}) — by project")
    if backlog:
        by_proj = {}
        for t in backlog:
            by_proj[t.get("project_id") or "?"] = by_proj.get(t.get("project_id") or "?", 0) + 1
        for proj in sorted(by_proj, key=str.lower):
            html += (
                f'<div style="padding:2px 0;color:#666;">'
                f'{_esc(proj)} <span style="color:#aaa;">— {by_proj[proj]}</span></div>'
            )
    else:
        html += '<div style="color:#888;">—</div>'

    return html


def _render_job_group(jobs: list | None, attention: dict) -> str:
    """Render one machine's launchd jobs: FAILED, then ATTENTION, then the rest.

    A nonzero exit is FAILED unless attention (the config's [[digest.attention]],
    keyed by (label, exit code)) lists that exact pair, in which case it renders
    as ATTENTION with the report to read. #7755: a monitor whose documented
    "needs attention" exit showed as FAILED every morning trained everyone to
    ignore FAILED; an unlisted pair is never downgraded. None → notice.
    """
    if jobs is None:
        return (
            '<div style="color:#8a6d1f;">⚠️ no job data this run.</div>'
        )
    if not jobs:
        return '<div style="color:#888;">No tracked jobs found.</div>'

    def classify(j):
        if j.get("pid") is not None:
            return ("🟢", "running", 3)
        last_exit = j.get("last_exit")
        if last_exit == 0:
            return ("🟢", "ok", 3)
        if last_exit is None:
            return ("⚪", "never run", 2)
        report = attention.get((j.get("label", ""), last_exit))
        if report is not None:
            return ("🟡", f"ATTENTION (exit {last_exit}) — see {report}", 1)
        return ("🔴", f"FAILED (exit {last_exit})", 0)

    rows = ""
    for j in sorted(jobs, key=lambda x: (classify(x)[2], x.get("label", ""))):
        dot, state, _ = classify(j)
        short = j.get("label", "?").split(".")[-1]
        color = "#8a1f1f" if dot == "🔴" else ("#8a6d1f" if dot == "🟡" else "#333")
        rows += (
            f'<tr><td style="padding:3px 10px;">{dot}</td>'
            f'<td style="padding:3px 10px;">{_esc(short)}</td>'
            f'<td style="padding:3px 10px;color:{color};">{_esc(state)}</td></tr>'
        )
    return f'<table style="border-collapse:collapse;width:100%;font-size:13px;">{rows}</table>'


def render_jobs_section(macbook_jobs: list | None, mini_jobs: list | None, has_mini: bool = True,
                        attention: dict | None = None) -> str:
    """Scheduled jobs for each machine, each its own subgroup; the Mini only when one is configured."""
    attention = attention or {}
    mini = ('<div style="margin:12px 0 2px;font-weight:600;color:#333;">🖥️ Mac Mini</div>'
            + _render_job_group(mini_jobs, attention)) if has_mini else ""
    return (
        '<div style="margin:6px 0 2px;font-weight:600;color:#333;">💻 MacBook</div>'
        + _render_job_group(macbook_jobs, attention)
        + mini
    )


def render_mini_section(mini_data: dict | None, ignore: set) -> str:
    """Render the Mac Mini section body from mini_scan.py output.

    Unreachable → a notice (not silence). Reachable → a heartbeat line proving
    the scan ran, the active projects, and any stale (>= STALE_DAYS) as warnings.
    """
    if mini_data is None:
        return (
            '<div style="color:#8a6d1f;background:#fffbe6;border:1px solid #e6d999;'
            'border-radius:6px;padding:10px;">⚠️ Mac Mini unreachable this run — no '
            'data. (Retried 3×; the digest still sent rather than waiting.)</div>'
        )
    if mini_data.get("error"):
        return (
            f'<div style="color:#8a1f1f;">⚠️ Mini scan error: {_esc(mini_data["error"])}</div>'
        )

    projects = [p for p in mini_data.get("projects", []) if p["name"] not in ignore]
    active = [p for p in projects if p["days_since"] < STALE_DAYS]
    stale = [p for p in projects if p["days_since"] >= STALE_DAYS]

    def _age(p):
        d = p["days_since"]
        return "today" if d == 0 else ("1 day ago" if d == 1 else f"{d} days ago")

    heartbeat = (
        f'<div style="color:#888;font-size:12px;margin-bottom:8px;">'
        f'Scanned {len(projects)} projects · {_esc(mini_data.get("scanned_at", ""))}</div>'
    )

    active_html = ""
    if active:
        rows = "".join(
            f'<tr><td style="padding:4px 10px;">🟢</td>'
            f'<td style="padding:4px 10px;"><strong>{_esc(p["name"])}</strong> '
            f'<span style="color:#888;">— active {_esc(_age(p))}</span></td></tr>'
            for p in active
        )
        active_html = f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'

    stale_html = ""
    if stale:
        rows = "".join(
            f'<tr><td style="padding:4px 10px;">🟡</td>'
            f'<td style="padding:4px 10px;"><strong>{_esc(p["name"])}</strong> '
            f'<span style="color:#333;">no activity in {_esc(p["days_since"])} days</span></td></tr>'
            for p in stale
        )
        stale_html = (
            '<div style="margin-top:10px;color:#8a6d1f;font-size:13px;">Stale (60+ days):</div>'
            f'<table style="border-collapse:collapse;width:100%;">{rows}</table>'
        )

    if not active and not stale:
        return heartbeat + '<div style="color:#2e7d32;">✅ Nothing to report.</div>'
    return heartbeat + active_html + stale_html


def _when(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days > 1:
        return f"in {days} days"
    if days == -1:
        return "missed yesterday"
    return f"missed {-days} days ago"


def render_deadlines_section(deadlines: list | None) -> str:
    """Rows for due or recently missed events; '' when there are none.

    ``None`` means the calendar could not be read, which is shown rather than
    hidden: an empty section must never stand in for a failed fetch.
    """
    if deadlines is None:
        return (
            '<div style="background:#fff3f3;border:1px solid #e0b4b4;border-radius:6px;'
            'padding:12px;color:#8a1f1f;"><strong>⚠️ Could not read the calendar.</strong> '
            'Upcoming deadlines may be missing from this email.</div>'
        )
    if not deadlines:
        return ""
    rows = []
    for e in deadlines:
        unreadable = e["days"] is None
        urgent = unreadable or e["days"] <= DEADLINE_SUBJECT_DAYS
        colour = "#8a1f1f" if urgent else "#555"
        when = e["problem"] if unreadable else _when(e["days"])
        time_part = f" {_esc(str(e['event_time']))}" if e.get("event_time") else ""
        project = f" · {_esc(str(e['project_id']))}" if e.get("project_id") else ""
        rows.append(
            '<tr>'
            f'<td style="padding:4px 10px;white-space:nowrap;color:{colour};">'
            f'<strong>{_esc(when)}</strong></td>'
            f'<td style="padding:4px 10px;white-space:nowrap;color:#888;">'
            f'{_esc(str(e.get("event_date", ""))[:10])}{time_part}</td>'
            f'<td style="padding:4px 10px;">{_esc(str(e.get("title", "")))}'
            f'<span style="color:#888;">{project}</span></td>'
            '</tr>'
        )
    return '<table style="border-collapse:collapse;width:100%;font-size:13px;">' + "".join(rows) + "</table>"


def render_html(
    macbook_alerts: list, mini_data: dict | None, mini_ignore: set,
    tasks: list | None, jobs: list | None,
    degraded_reason: str | None, sent_at: str, deadlines: list | None = (), has_mini: bool = True,
    attention: dict | None = None,
) -> str:
    """Render the full email: cards + jobs, then machine-sectioned alerts (the Mini's only if has_mini)."""
    if degraded_reason:
        macbook_body = (
            f'<div style="background:#fff3f3;border:1px solid #e0b4b4;border-radius:6px;'
            f'padding:12px;color:#8a1f1f;">'
            f'<strong>⚠️ Could not read alerts on this machine.</strong><br>'
            f'{degraded_reason}<br>'
            f'<span style="font-size:12px;color:#a55;">The dashboard being unreachable is '
            f'itself a problem — check that the project-tracker launchd job is running.</span>'
            f'</div>'
        )
    elif not macbook_alerts:
        macbook_body = (
            '<div style="color:#2e7d32;padding:8px 0;">✅ Nothing broken. All quiet.</div>'
        )
    else:
        macbook_body = (
            '<table style="border-collapse:collapse;width:100%;">'
            + _render_alert_rows(macbook_alerts)
            + '</table>'
        )

    mini_html = f"""
  <h3 style="border-bottom:2px solid #333;padding-bottom:4px;margin-top:28px;">
    🖥️ Mac Mini
  </h3>
  {render_mini_section(mini_data, mini_ignore)}
""" if has_mini else ""
    deadlines_body = render_deadlines_section(deadlines)
    deadlines_html = (
        '  <h3 style="border-bottom:2px solid #333;padding-bottom:4px;">📅 Deadlines</h3>\n  '
        + deadlines_body
        + '\n  <div style="height:16px;"></div>\n'
        if deadlines_body else ""
    )

    return f"""\
<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
             color:#222;max-width:640px;margin:0 auto;padding:16px;">
  <h2 style="margin:0 0 4px;">Portfolio Morning Digest</h2>
  <div style="color:#888;font-size:13px;margin-bottom:20px;">{sent_at}</div>

{deadlines_html}
  <h3 style="border-bottom:2px solid #333;padding-bottom:4px;">🎫 Cards</h3>
  {render_cards_section(tasks)}

  <h3 style="border-bottom:2px solid #333;padding-bottom:4px;margin-top:28px;">⏰ Scheduled Jobs</h3>
  {render_jobs_section(jobs, mini_data.get("jobs") if mini_data else None, has_mini, attention)}

  <h3 style="border-bottom:2px solid #333;padding-bottom:4px;margin-top:28px;">💻 MacBook</h3>
  {macbook_body}
{mini_html}
  <div style="color:#bbb;font-size:11px;margin-top:32px;border-top:1px solid #eee;padding-top:8px;">
    Sent by the portfolio alert digest · leave projects out with
    <code>ignore</code> and <code>mini_ignore</code> in the local config's [digest]
  </div>
</body>
</html>
"""


# --- Send -------------------------------------------------------------------

def send_email(subject: str, html: str, digest: DigestIntegration) -> None:
    """Send via Resend. Retries; raises after exhausting attempts."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        # Secrets come from Doppler. If it's missing, crash loudly — do not
        # invent a fallback and do not send from nowhere.
        raise RuntimeError(
            "RESEND_API_KEY not set — run under `doppler run` with the project that holds it"
        )

    body = json.dumps(
        {"from": digest.sender, "to": [digest.to], "subject": subject, "html": html}
    ).encode("utf-8")

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                RESEND_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": BROWSER_UA,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                code = resp.getcode()
                if code in (200, 201):
                    log(f"email sent (HTTP {code}): {subject}")
                    return
                raise RuntimeError(f"Resend returned HTTP {code}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last_err = f"HTTP {exc.code}: {detail}"
            log(f"WARN send attempt {attempt}/{MAX_RETRIES} failed: {last_err}")
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            log(f"WARN send attempt {attempt}/{MAX_RETRIES} failed: {last_err}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS)
    raise RuntimeError(f"failed to send after {MAX_RETRIES} attempts: {last_err}")


# --- Main -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Entry point. ``argv`` defaults to ``sys.argv[1:]``; tests pass it in."""
    parser = argparse.ArgumentParser(description="Portfolio alert digest (Phase 1).")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the email instead of sending."
    )
    args = parser.parse_args(argv)
    try:
        digest = digest_integration()
    except ValueError as exc:
        log(f"ERROR {exc}")
        return 1
    if digest is None:
        log(DIGEST_NOT_CONFIGURED.format(path=local_config_path()))
        return 0
    # RESEND_API_KEY lives in the configured Doppler project: run this script
    # again under `doppler run`, once (the marker stops it repeating).
    if digest.doppler_project and not os.environ.get("PT_DIGEST_UNDER_DOPPLER"):
        os.environ["PT_DIGEST_UNDER_DOPPLER"] = "1"
        command = ["doppler", "run", "--project", digest.doppler_project, "--config", digest.doppler_config,
                   "--", sys.executable, str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)]
        try:
            os.execvp(command[0], command)
        except OSError as exc:
            log(f"ERROR could not run under doppler: {exc}")
            return 1

    macbook_ignore, mini_ignore = digest.ignore, digest.mini_ignore
    sent_at = datetime.now().strftime("%A, %B %d %Y · %-I:%M %p")

    degraded_reason = None
    alerts = []
    try:
        raw = fetch_alerts()
        alerts = [a for a in raw if a.get("project_id") not in macbook_ignore]
        dropped = len(raw) - len(alerts)
        log(f"{len(alerts)} MacBook alerts after ignore filter ({dropped} dropped)")
    except Exception as exc:  # noqa: BLE001
        degraded_reason = str(exc)
        log(f"ERROR entering degraded mode: {exc}")

    mini_data = fetch_mini_data(digest.mini_host, digest.job_label_prefixes) if digest.mini_host else None
    tasks = fetch_tasks()
    jobs = fetch_scheduled_jobs(digest.job_label_prefixes)
    events = fetch_calendar_events()
    deadlines = None
    if events is not None:
        try:
            deadlines = upcoming_deadlines(events, date.today())
        except Exception as exc:  # noqa: BLE001
            # Shown in the email as "Could not read the calendar"; never a crash
            # that stops the whole digest from going out.
            log(f"ERROR calendar events could not be read: {exc}")

    if degraded_reason:
        subject = ", ".join(
            ["[Project Alerts] ⚠️ Digest degraded"]
            + _deadline_subject_parts(deadlines)
        )
    else:
        subject = build_subject(alerts, deadlines)
    html = render_html(
        alerts, mini_data, mini_ignore, tasks, jobs, degraded_reason, sent_at, deadlines,
        has_mini=digest.mini_host is not None, attention=digest.attention,
    )

    if args.dry_run:
        print(f"Subject: {subject}\nTo: {digest.to}\nFrom: {digest.sender}\n")
        print(html)
        return 0

    try:
        send_email(subject, html, digest)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR could not send digest: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
