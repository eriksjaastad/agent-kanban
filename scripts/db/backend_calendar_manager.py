"""
Calendar Manager — AI-first calendar for project-tracker.

Design philosophy:
- Every field is optional except title + date (minimal barrier to entry)
- Agents can do everything humans can; the 7am digest (scripts/alert_digest.py) reports deadlines
- Flexible: add new event types / fields without breaking existing rows
- Cron jobs are surfaced alongside events (they're scheduled too)
- Extensible metadata field for future fields (dict stored as JSON)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from .pt_id import next_id as pt_next_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # Deserialize metadata JSON if present — warn on corruption, don't silently swallow
    if d.get("metadata") and isinstance(d["metadata"], str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError) as exc:
            import warnings
            warnings.warn(
                f"calendar_events row {d.get('id')}: corrupted metadata JSON ({exc}). "
                "Resetting to empty dict.",
                stacklevel=2,
            )
            d["metadata"] = {}
    return d


VALID_EVENT_TYPES = {"reminder", "deadline", "milestone", "meeting", "recurring"}
VALID_RECURRENCE = {None, "daily", "weekly", "monthly"}


# ASCII only: str.isdigit-style \d also matches digits such as "٠" or "６",
# which then sort and parse unlike the times they look like.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d", re.ASCII)


def validate_event_date(value: str) -> str:
    """Return ``value`` if it is a real calendar date written YYYY-MM-DD, else raise ValueError.

    The one date/time validator for the calendar: the CLI (``pt calendar add`` /
    ``update``), the dashboard create endpoint and ``CalendarManager`` writes all
    use it. Digest and sort logic compare these strings, so a loose form such as
    ``2026-1-5`` is rejected along with impossible dates like ``2026-02-30``.
    """
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"event_date must be a real date written YYYY-MM-DD, got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"event_date must be a real date written YYYY-MM-DD, got {value!r}") from None
    return value


def validate_event_time(value: str) -> str:
    """Return ``value`` if it is a 24-hour HH:MM time, else raise ValueError."""
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise ValueError(f"event_time must be a 24-hour time written HH:MM, got {value!r}")
    return value


def validate_block(event_time: Optional[str], end_time: Optional[str]) -> None:
    """An end time needs a start time and comes after it on the same day (blocks don't cross midnight)."""
    if end_time is None:
        return
    validate_event_time(end_time)
    if event_time is None:
        raise ValueError("end_time needs an event_time to start from")
    validate_event_time(event_time)  # a stored time from before ASCII-only validation, too
    if end_time <= event_time:
        raise ValueError(f"end_time {end_time} must be after event_time {event_time}")


def _next_occurrence(event: Dict[str, Any], day: date) -> Optional[date]:
    """The first day on or after ``day`` that ``event`` happens: once on its date,
    or from it on daily, on its weekday, or on its day of the month (skipping
    months too short to have it)."""
    start = date.fromisoformat(event["event_date"])
    recurrence = event.get("recurrence")
    if recurrence is None:
        return start if start >= day else None
    day = max(day, start)
    if recurrence == "daily":
        return day
    if recurrence == "weekly":
        return day + timedelta(days=(start.weekday() - day.weekday()) % 7)
    year, month = day.year, day.month
    for _ in range(12):  # every day of the month 1-31 occurs within a year
        try:
            candidate = date(year, month, start.day)
        except ValueError:
            candidate = None
        if candidate and candidate >= day:
            return candidate
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def validate_event_type(event_type: str) -> tuple[bool, str]:
    if event_type not in VALID_EVENT_TYPES:
        return False, f"Invalid event_type '{event_type}'. Must be one of: {', '.join(sorted(VALID_EVENT_TYPES))}"
    return True, ""



# ---------------------------------------------------------------------------
# CalendarManager
# ---------------------------------------------------------------------------

class CalendarManager:
    """Manages calendar_events, calendar_event_tasks, and surfaces cron_jobs.

    Designed to be used standalone OR via an existing db_path.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent.parent))
            try:
                from scripts.config import DATABASE_PATH
                db_path = DATABASE_PATH
            except ImportError:
                db_path = Path("data/tracker.db")
        self.db_path = Path(db_path)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Write — events
    # ------------------------------------------------------------------

    def add_event(
        self,
        title: str,
        event_date: str,
        *,
        description: Optional[str] = None,
        event_time: Optional[str] = None,
        end_time: Optional[str] = None,
        event_type: str = "reminder",
        recurrence: Optional[str] = None,
        project_id: Optional[str] = None,
        machine: Optional[str] = None,
        prompt: Optional[str] = None,
        notify_before_minutes: int = 60,
        created_by: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Add a calendar event. Returns the new event ID.

        An empty ``event_time`` means no time and is stored as NULL.
        """
        validate_event_date(event_date)
        if event_time == "":
            event_time = None
        if event_time is not None:
            validate_event_time(event_time)
        end_time = end_time or None
        validate_block(event_time, end_time)
        ok, err = validate_event_type(event_type)
        if not ok:
            raise ValueError(err)
        if recurrence not in VALID_RECURRENCE:
            raise ValueError(f"Invalid recurrence '{recurrence}'. Must be one of: daily, weekly, monthly, or null")

        now = _now()
        event_id = pt_next_id(self.db_path)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO calendar_events
                    (id, title, description, event_date, event_time, end_time, event_type, recurrence,
                     project_id, machine, prompt, notify_before_minutes,
                     created_by, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    title, description, event_date, event_time, end_time, event_type, recurrence,
                    project_id, machine, prompt, notify_before_minutes,
                    created_by, json.dumps(metadata or {}), now, now,
                ),
            )
            conn.commit()
            return event_id

    def update_event(self, event_id: int, **updates: Any) -> bool:
        """Update any fields on an event. Returns True if event was found."""
        allowed = {
            "title", "description", "event_date", "event_time", "end_time", "event_type",
            "recurrence", "project_id", "machine", "prompt", "notify_before_minutes",
            "status", "created_by", "metadata",
        }
        bad = set(updates) - allowed
        if bad:
            raise ValueError(f"Unknown field(s): {bad}")
        if not updates:
            return True

        if "event_date" in updates:
            validate_event_date(updates["event_date"])
        if updates.get("event_time") == "":
            updates["event_time"] = None  # an empty time clears it
        if updates.get("event_time") is not None:
            validate_event_time(updates["event_time"])
        if updates.get("end_time") == "":
            updates["end_time"] = None  # an empty end clears it
        if "event_type" in updates:
            ok, err = validate_event_type(updates["event_type"])
            if not ok:
                raise ValueError(err)
        if "metadata" in updates and isinstance(updates["metadata"], dict):
            updates["metadata"] = json.dumps(updates["metadata"])

        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [event_id]

        with self._conn() as conn:
            # Read, check and write the time pair under one write lock, so a
            # concurrent update can't pair this change with a stale value.
            conn.execute("BEGIN IMMEDIATE")
            try:
                if "event_time" in updates or "end_time" in updates:
                    row = conn.execute(
                        "SELECT event_date, event_time, end_time FROM calendar_events WHERE id = ?", (event_id,)
                    ).fetchone()
                    if row is None:
                        conn.rollback()
                        return False
                    end = updates.get("end_time", row["end_time"])
                    validate_block(updates.get("event_time", row["event_time"]), end)
                    if end is not None:  # a block's date is parsed, so an old stored one must be canonical
                        validate_event_date(updates.get("event_date", row["event_date"]))
                cursor = conn.execute(
                    f"UPDATE calendar_events SET {set_clause} WHERE id = ?", values
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return cursor.rowcount > 0

    def mark_done(self, event_id: int) -> bool:
        return self.update_event(event_id, status="done")

    def cancel_event(self, event_id: int) -> bool:
        return self.update_event(event_id, status="cancelled")

    def link_task(self, event_id: int, task_id: int, link_type: str = "related") -> None:
        """Link a task to an event (bidirectional via join table)."""
        valid_link_types = {"related", "deadline-for", "blocks"}
        if link_type not in valid_link_types:
            raise ValueError(f"link_type must be one of: {valid_link_types}")
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO calendar_event_tasks (event_id, task_id, link_type) VALUES (?, ?, ?)",
                (event_id, task_id, link_type),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Read — events
    # ------------------------------------------------------------------

    def events_on(self, day: date) -> Dict[str, List[Dict[str, Any]]]:
        """Active events happening on ``day``, recurring ones included, untimed first, then by time.

        An event whose stored date or time is unreadable (written before the
        strict validators) can't be placed on a day, so it is returned under
        ``unreadable`` instead of failing the whole day or vanishing.
        """
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM calendar_events WHERE status = 'active'").fetchall()
        found: Dict[str, List[Dict[str, Any]]] = {"events": [], "unreadable": []}
        for event in (_row_to_dict(row) for row in rows):
            try:
                validate_event_date(event["event_date"])
                if event["event_time"] is not None:
                    validate_event_time(event["event_time"])
            except ValueError:
                found["unreadable"].append(event)
                continue
            if _next_occurrence(event, day) == day:
                found["events"].append(event)
        found["events"].sort(key=lambda e: (e["event_time"] is not None, e["event_time"] or ""))
        return found

    def blocks_at(self, when: datetime) -> Dict[str, Optional[Dict[str, Any]]]:
        """The block ``when`` falls in and the next one to start, from active timed events with an end.

        Everything is this machine's wall clock: event times are written in it
        and ``when`` is read from it, so there is no time zone to reconcile.
        Each result carries naive local ``start`` and ``end`` datetimes.
        Overlapping blocks resolve to the one that started last; ``next`` is
        the earliest next occurrence over all blocks, however far ahead.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM calendar_events WHERE status = 'active'"
                " AND event_time IS NOT NULL AND end_time IS NOT NULL"
            ).fetchall()
        events = [_row_to_dict(row) for row in rows]
        now = when.replace(second=0, microsecond=0, tzinfo=None)

        def at(event: Dict[str, Any], day: date) -> Dict[str, Any]:
            def clock(hhmm: str) -> datetime:
                return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())
            return {**event, "start": clock(event["event_time"]), "end": clock(event["end_time"])}

        today = now.date()
        todays = sorted((at(e, today) for e in events if _next_occurrence(e, today) == today), key=lambda e: e["start"])
        live = [e for e in todays if e["start"] <= now < e["end"]]
        upcoming = []
        for event in events:
            day = _next_occurrence(event, today)
            if day == today and at(event, today)["start"] <= now:
                day = _next_occurrence(event, today + timedelta(days=1))
            if day is not None:
                upcoming.append(at(event, day))
        found: Dict[str, Optional[Dict[str, Any]]] = {
            "current": live[-1] if live else None,
            "next": min(upcoming, key=lambda e: e["start"]) if upcoming else None,
        }
        return found

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM calendar_events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        event = _row_to_dict(row)
        event["linked_tasks"] = self._get_linked_tasks(event_id)
        return event

    def get_events(
        self,
        *,
        days: int = 7,
        from_date: Optional[str] = None,
        project_id: Optional[str] = None,
        machine: Optional[str] = None,
        event_type: Optional[str] = None,
        status: str = "active",
        include_all: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get events. Defaults to next N days. Pass include_all=True to ignore date window."""
        query = "SELECT * FROM calendar_events WHERE 1=1"
        params: list = []

        if not include_all:
            from datetime import timedelta
            start = from_date or datetime.now(timezone.utc).date().isoformat()
            end_date = (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()
            query += " AND event_date >= ? AND event_date <= ?"
            params += [start, end_date]

        if status and not include_all:
            query += " AND status = ?"
            params.append(status)

        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if machine:
            query += " AND machine = ?"
            params.append(machine)

        query += " ORDER BY event_date ASC, event_time ASC NULLS LAST"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def _get_linked_tasks(self, event_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT t.id, t.text, t.status, t.priority, t.project_id, cet.link_type
                FROM tasks t
                JOIN calendar_event_tasks cet ON cet.task_id = t.id
                WHERE cet.event_id = ?
                ORDER BY t.id
                """,
                (event_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Cron jobs — surface existing jobs alongside calendar events
    # ------------------------------------------------------------------

    def get_cron_jobs(
        self,
        *,
        project_id: Optional[str] = None,
        machine: Optional[str] = None,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return cron jobs, filtered by project and/or machine."""
        query = "SELECT * FROM cron_jobs WHERE 1=1"
        params: list = []

        if active_only:
            query += " AND is_active = 1"
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if machine:
            query += " AND machine = ?"
            params.append(machine)

        query += " ORDER BY project_id, schedule"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def add_cron_job(
        self,
        project_id: str,
        schedule: str,
        command: str,
        description: Optional[str] = None,
    ) -> int:
        """Add a cron job and return its ID."""
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO cron_jobs (project_id, schedule, command, description, is_active)
                   VALUES (?, ?, ?, ?, 1)""",
                (project_id, schedule, command, description),
            )
            conn.commit()
            return cursor.lastrowid
