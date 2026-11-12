"""The Reminders API behind the dashboard's Reminders page (#8282)."""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from addons.reminders.db import RemindersDB
from dashboard.common import _serialized, logger

router = APIRouter()


class ReminderRequest(BaseModel):
    name: str
    purpose: Optional[str] = None
    location: Optional[str] = None


def _reminders_db_error(action: str, exc: Exception) -> HTTPException:
    """Log the failure; a missing table says how to fix it."""
    logger.exception("Failed to %s", action)
    if "no such table" in str(exc):
        return HTTPException(
            status_code=500, detail="Table reminders is missing; run `pt db migrate`."
        )
    return HTTPException(status_code=500, detail=f"Failed to {action}")


@router.get("/api/reminders")
def list_reminders():
    """Active reminders in the order added, then built-in ones newest first."""
    try:
        return RemindersDB().list_reminders()
    except Exception as exc:
        raise _reminders_db_error("list reminders", exc)


@router.post("/api/reminders", status_code=201)
@_serialized
def add_reminder(payload: ReminderRequest):
    try:
        reminder = RemindersDB().add_reminder(
            payload.name, payload.purpose, payload.location
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Name is required")
    except Exception as exc:
        raise _reminders_db_error("add reminder", exc)
    return {"reminder": reminder}


@router.post("/api/reminders/{reminder_id}/built-in")
@_serialized
def mark_reminder_built_in(reminder_id: int):
    try:
        reminder = RemindersDB().mark_reminder_built_in(reminder_id)
    except Exception as exc:
        raise _reminders_db_error("mark reminder built in", exc)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"reminder": reminder}


@router.delete("/api/reminders/{reminder_id}")
@_serialized
def remove_reminder(reminder_id: int):
    try:
        reminder = RemindersDB().remove_reminder(reminder_id)
    except Exception as exc:
        raise _reminders_db_error("remove reminder", exc)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"reminder": reminder}
