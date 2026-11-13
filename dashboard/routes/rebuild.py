"""Tracked background rebuilds: a job id the page polls at /api/rebuild-status for real completion."""

import uuid
from datetime import datetime
from typing import Callable, Dict, List

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from dashboard.common import logger

router = APIRouter()

# Called after a rebuild succeeds, so a cache built from the old output can drop itself.
AFTER_REBUILD: List[Callable[[], None]] = []

# Tracks in-flight rebuild jobs so the frontend can poll real completion
# status instead of guessing via node-count diffs. Job dicts live for the
# lifetime of the process; they're small and self-expire on restart.
_rebuild_jobs: Dict[str, Dict] = {}


def _run_tracked_rebuild(job_id: str, fn) -> None:
    """Run a rebuild function with status tracking, then call AFTER_REBUILD on success."""
    job = _rebuild_jobs.get(job_id)
    if job is None:
        return
    job["status"] = "running"
    job["started_at"] = datetime.now().isoformat()
    try:
        fn()
        job["status"] = "done"
        job["error"] = None
        for after in AFTER_REBUILD:
            after()
    except Exception as e:
        logger.error(f"Rebuild job {job_id} ({job.get('kind')}) failed: {e}")
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        job["finished_at"] = datetime.now().isoformat()


def _create_rebuild_job(kind: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    _rebuild_jobs[job_id] = {
        "job_id": job_id,
        "kind": kind,
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "error": None,
    }
    return job_id


@router.get("/api/rebuild-status/{job_id}")
async def get_rebuild_status(job_id: str):
    """Poll status of a rebuild job. Returns 404 if unknown job."""
    job = _rebuild_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job_id"}, status_code=404)
    return job
