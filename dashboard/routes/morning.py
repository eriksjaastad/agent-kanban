"""GET /api/morning: the Morning page snapshot."""

from fastapi import APIRouter, HTTPException

from dashboard.common import logger

router = APIRouter()


@router.get("/api/morning")
def get_morning_snapshot():
    try:
        from dashboard.morning import MorningPlanError, morning_snapshot
        return morning_snapshot()
    except MorningPlanError as exc:
        logger.error("Failed to load morning plan: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
