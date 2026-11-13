"""The /agentic page's API: summary and timeline markers."""

import json
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

from db.manager import DatabaseManager
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from dashboard.common import (
    MARKERS_PATH,
    _serialized,
    logger,
)
from scripts.config import module_present

router = APIRouter()


@router.get("/api/agentic/summary")
def agentic_summary(days: int = 30, project_id: Optional[str] = None):
    """Get agentic workflow metrics from task_history.

    Tracks Review -> In Progress bounces, Review -> Done promotions, and
    In Progress -> Review entries over time.
    """
    try:
        db = DatabaseManager()

        if days <= 0:
            days = 30

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days - 1)
        start_iso = start_date.isoformat()

        rows = db.task_history_daily(start_iso=start_iso, project_id=project_id)

        row_map = {row["date"]: row for row in rows}

        markers = []
        markers_error = None
        try:
            markers = [
                m for m in _load_markers()
                if isinstance(m, dict) and m.get("date") and m.get("label")
            ]
        except MarkersUnreadable as exc:
            markers_error = str(exc)
        series = []
        totals = {"review_bounces": 0, "review_promotions": 0, "review_entries": 0}

        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.isoformat()
            row = row_map.get(date_str)
            entry = {
                "date": date_str,
                "review_bounces": int(row["review_bounces"]) if row else 0,
                "review_promotions": int(row["review_promotions"]) if row else 0,
                "review_entries": int(row["review_entries"]) if row else 0,
            }
            totals["review_bounces"] += entry["review_bounces"]
            totals["review_promotions"] += entry["review_promotions"]
            totals["review_entries"] += entry["review_entries"]
            series.append(entry)
            current_date += timedelta(days=1)

        review_total = totals["review_bounces"] + totals["review_promotions"]
        bounce_rate = (totals["review_bounces"] / review_total) if review_total else 0.0
        promotion_rate = (totals["review_promotions"] / review_total) if review_total else 0.0

        filtered_markers = [
            m for m in markers
            if start_date.isoformat() <= m["date"] <= end_date.isoformat()
        ]

        return {
            "summary": {
                "review_bounces": totals["review_bounces"],
                "review_promotions": totals["review_promotions"],
                "review_entries": totals["review_entries"],
                "bounce_rate": bounce_rate,
                "promotion_rate": promotion_rate,
            },
            "series": series,
            "markers": filtered_markers,
            "markers_error": markers_error,
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "project_id": project_id,
            # The page draws its tool and Bash charts only when their routes are installed.
            "usage_stats": module_present("dashboard.routes.usage_stats"),
        }
    except Exception as e:
        logger.error(f"Error building agentic summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build agentic summary"
        )


class MarkersUnreadable(Exception):
    """The markers file exists but cannot be read as a JSON list."""


def _load_markers() -> list:
    """Load markers from disk.

    A missing file is the valid empty state. A present-but-unreadable file
    (OSError, invalid UTF-8, invalid JSON, or JSON that is not a list) raises
    MarkersUnreadable instead of returning []: treating corruption as an
    empty inbox would let the next POST overwrite every existing marker.
    """
    if not MARKERS_PATH.exists():
        return []
    try:
        data = json.loads(MARKERS_PATH.read_text())
    except OSError as exc:
        logger.error("Markers file %s cannot be read: %s", MARKERS_PATH, exc)
        raise MarkersUnreadable(f"cannot read {MARKERS_PATH}: {exc}") from exc
    except UnicodeDecodeError as exc:
        logger.error("Markers file %s is not valid UTF-8: %s", MARKERS_PATH, exc)
        raise MarkersUnreadable(f"{MARKERS_PATH} is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        logger.error("Markers file %s contains invalid JSON: %s", MARKERS_PATH, exc)
        raise MarkersUnreadable(f"invalid JSON in {MARKERS_PATH}: {exc}") from exc
    if not isinstance(data, list):
        logger.error(
            "Markers file %s contains a JSON %s, not a list",
            MARKERS_PATH, type(data).__name__,
        )
        raise MarkersUnreadable(
            f"{MARKERS_PATH} contains a JSON {type(data).__name__}, not a list"
        )
    return data


def _save_markers(markers: list) -> None:
    """Atomically write markers to disk."""
    MARKERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MARKERS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(markers, indent=2))
    tmp.replace(MARKERS_PATH)


class MarkerCreateRequest(BaseModel):
    date: str
    label: str
    source: str = "manual"
    agent: Optional[str] = None


class MarkerUpdateRequest(BaseModel):
    date: Optional[str] = None
    label: Optional[str] = None
    agent: Optional[str] = None


def _validate_marker_fields(date: Optional[str], label: Optional[str]) -> None:
    """Raise 400 if fields fail validation."""
    if date is not None:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if label is not None:
        label = label.strip()
        if not label:
            raise HTTPException(status_code=400, detail="label must not be empty")
        if len(label) > 120:
            raise HTTPException(status_code=400, detail="label must be 120 characters or fewer")


@router.get("/api/agentic/markers")
def get_markers():
    """Return all agentic markers."""
    try:
        return {"markers": _load_markers()}
    except MarkersUnreadable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post("/api/agentic/markers", status_code=201)
@_serialized
def create_marker(req: MarkerCreateRequest):
    """Create a new agentic marker."""
    _validate_marker_fields(req.date, req.label)
    try:
        markers = _load_markers()
    except MarkersUnreadable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    marker = {
        "id": str(uuid.uuid4()),
        "date": req.date,
        "label": req.label.strip(),
        "source": req.source if req.source in ("manual", "auto") else "manual",
        "agent": req.agent or None,
    }
    markers.append(marker)
    markers.sort(key=lambda m: m["date"])
    _save_markers(markers)
    return marker


@router.patch("/api/agentic/markers/{marker_id}")
@_serialized
def update_marker(marker_id: str, req: MarkerUpdateRequest):
    """Update an existing agentic marker by id."""
    _validate_marker_fields(req.date, req.label)
    try:
        markers = _load_markers()
    except MarkersUnreadable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    for m in markers:
        if m.get("id") == marker_id:
            if req.date is not None:
                m["date"] = req.date
            if req.label is not None:
                m["label"] = req.label.strip()
            if req.agent is not None:
                m["agent"] = req.agent
            markers.sort(key=lambda x: x["date"])
            _save_markers(markers)
            return m
    raise HTTPException(status_code=404, detail="Marker not found")


@router.delete("/api/agentic/markers/{marker_id}", status_code=204)
@_serialized
def delete_marker(marker_id: str):
    """Delete an agentic marker by id."""
    try:
        markers = _load_markers()
    except MarkersUnreadable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    updated = [m for m in markers if m.get("id") != marker_id]
    if len(updated) == len(markers):
        raise HTTPException(status_code=404, detail="Marker not found")
    _save_markers(updated)
    return None
