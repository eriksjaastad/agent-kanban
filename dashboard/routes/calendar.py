"""The calendar API: events, completing them, cron jobs and the scheduled-jobs view."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from db.manager import DatabaseManager
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.common import _serialized, _stringify_pt_ids, logger
from scripts.config import owner_name, owner_prefix
from scripts.config import projects_root as config_projects_root
from scripts.scheduled_jobs import scheduled_jobs

router = APIRouter()


class CalendarEventCreate(BaseModel):
    title: str
    event_date: str
    event_time: Optional[str] = None
    event_type: str = "reminder"
    project_id: Optional[str] = None
    machine: Optional[str] = None
    prompt: Optional[str] = None
    description: Optional[str] = None
    notify_before_minutes: int = 60
    recurrence: Optional[str] = None
    created_by: str = "human"


def _get_cal_manager():
    from db.calendar_manager import CalendarManager
    return CalendarManager()


@router.get("/api/calendar/events")
def api_calendar_events(
    days: int = 30,
    project_id: Optional[str] = None,
    machine: Optional[str] = None,
    event_type: Optional[str] = None,
    include_all: bool = False,
):
    """Return upcoming calendar events."""
    days = max(1, min(days, 730))  # clamp: 1–730 days
    try:
        cm = _get_cal_manager()
        events = cm.get_events(
            days=days,
            project_id=project_id,
            machine=machine,
            event_type=event_type,
            include_all=include_all,
        )
        return {"events": [_stringify_pt_ids(e) for e in events], "total": len(events)}
    except Exception as e:
        logger.error(f"Calendar events error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/calendar/today")
def api_calendar_today():
    """Today's events on this machine's clock, recurring ones included, for the calendar's Today view."""
    from datetime import date

    today = date.today()
    try:
        found = _get_cal_manager().events_on(today)
    except Exception as e:
        logger.error(f"Calendar today error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"date": today.isoformat(), **{k: [_stringify_pt_ids(e) for e in v] for k, v in found.items()}}


@router.post("/api/calendar/events")
@_serialized
def api_calendar_create(payload: CalendarEventCreate):
    """Create a calendar event."""
    try:
        cm = _get_cal_manager()
        event_id = cm.add_event(
            title=payload.title,
            event_date=payload.event_date,
            event_time=payload.event_time,
            event_type=payload.event_type,
            project_id=payload.project_id,
            machine=payload.machine,
            prompt=payload.prompt,
            description=payload.description,
            notify_before_minutes=payload.notify_before_minutes,
            recurrence=payload.recurrence,
            created_by=payload.created_by,
        )
        return {"id": str(event_id), "title": payload.title, "event_date": payload.event_date}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Calendar create error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/calendar/events/{event_id}/done")
@_serialized
def api_calendar_done(event_id: int):
    """Mark calendar event as done."""
    try:
        cm = _get_cal_manager()
        if cm.mark_done(event_id):
            return {"ok": True}
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Calendar done error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/calendar/crons")
def api_calendar_crons(
    project_id: Optional[str] = None,
    machine: Optional[str] = None,
):
    """Return all active cron jobs with machine designations."""
    try:
        cm = _get_cal_manager()
        crons = cm.get_cron_jobs(project_id=project_id, machine=machine)
        return {"cron_jobs": crons, "total": len(crons)}
    except Exception as e:
        logger.error(f"Calendar crons error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/scheduled-jobs")
def api_scheduled_jobs():
    """Every launchd and crontab job on this Mac, live, including switched-off ones (#8377).

    Read-only. Returns ``{"jobs", "errors"}``; a source that could not be read is
    in ``errors`` with 200, so the page shows what it could read and what it could not.

    A job with a last run also gets ``needed_owner`` (#8847): that run failed, or an
    open card waiting on the owner names the job's label (listed in ``owner_cards``;
    the owner's name and prefix are in ``owner`` and ``owner_prefix``). launchd keeps
    neither run history nor run times, so this judges the last run only, and a manual
    rerun can't be seen.
    """
    data = scheduled_jobs(Path.home(), config_projects_root())
    data["owner"] = owner_name()
    prefix = data["owner_prefix"] = owner_prefix(data["owner"])
    db = DatabaseManager()
    cards = db.get_owner_cards(["Backlog", "To Do", "In Progress", "Review"], prefix)
    for job in data["jobs"]:
        if job["last_exit"] is not None:
            job["owner_cards"] = [card["display_id"] for card in cards if job["label"] in card["text"]]
            job["needed_owner"] = job["last_exit"] != 0 or bool(job["owner_cards"])
    now = datetime.now().astimezone()
    # A string bound one day wide, so an offset change (DST) cannot drop a run; the exact cut is below.
    runs = db.loop_runs_since((now - timedelta(days=HISTORY_DAYS + 1)).isoformat(timespec="seconds"))
    data["history"] = run_history(runs, db.get_owner_cards(ALL_STATUSES, prefix, include_archived=True), now)
    return data


HISTORY_DAYS = 30
ALL_STATUSES = ["Backlog", "To Do", "In Progress", "Review", "Done", "Cancelled"]


def run_history(runs: list, cards: list, now: datetime) -> dict:
    """Recorded runs of the last 30 days (scripts/record-run.sh, #8847), per job and per day.

    A finished run needed the owner if it failed, or if a card waiting on them (any status,
    archived ones too) naming its label was created within 24 hours after it started (a card's notes are not dated
    per line, so a note added later is not seen). A run still marked running is
    counted apart: it is in progress, was killed, or its end was not recorded.
    """
    start = (now - timedelta(days=HISTORY_DAYS - 1)).date()
    opened = [(card["text"], datetime.fromisoformat(card["created_at"])) for card in cards]
    jobs: dict = {}
    days = {start + timedelta(days=n): {"runs": 0, "unattended": 0} for n in range(HISTORY_DAYS)}
    running = 0
    for run in runs:
        began = datetime.fromisoformat(run["started_at"]).astimezone().replace(tzinfo=None)
        if began.date() not in days:  # before the window, or after now on a clock set back
            continue
        if run["status"] == "running":
            running += 1
            continue
        needed = run["status"] != "success" or any(
            run["loop_name"] in text and began <= created < began + timedelta(hours=24) for text, created in opened)
        for tally in (jobs.setdefault(run["loop_name"], {"runs": 0, "unattended": 0}), days[began.date()]):
            tally["runs"] += 1
            tally["unattended"] += not needed
    return {"days": HISTORY_DAYS, "running": running, "jobs": jobs,
            "daily": [{"date": day.isoformat(), **tally} for day, tally in days.items()]}
