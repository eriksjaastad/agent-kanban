"""The Jobs and Volunteer API: listings, dismissals, submissions and agent prompts."""

import shlex
from typing import Optional

from db.jobs import JobsMigrationPendingError
from db.manager import DatabaseManager
from discovery.agent_registry import run_agent_command
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from dashboard.common import _require_local_admin_request, _serialized, logger
from scripts.config import tailor_rules

router = APIRouter()


class JobSubmissionRequest(BaseModel):
    submitted_at: Optional[str] = None
    resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    notes: Optional[str] = None


@router.get("/api/jobs")
def list_open_jobs():
    """Ranked open listings; ones older than 30 days come back separately."""
    try:
        ranked = DatabaseManager().get_ranked_open_jobs()
        return {"jobs": ranked["fresh"], "stale": ranked["stale"]}
    except Exception:
        logger.exception("Failed to list open jobs")
        raise HTTPException(status_code=500, detail="Failed to list jobs")


@router.get("/api/jobs/submissions")
def list_job_submissions():
    try:
        return {"jobs": DatabaseManager().get_submitted_jobs()}
    except Exception:
        logger.exception("Failed to list job submissions")
        raise HTTPException(status_code=500, detail="Failed to list submissions")


@router.get("/api/jobs/stats")
def job_stats():
    try:
        return DatabaseManager().get_job_stats()
    except Exception:
        logger.exception("Failed to read job stats")
        raise HTTPException(status_code=500, detail="Failed to read job stats")


@router.delete("/api/jobs/{job_id}")
@_serialized
def dismiss_job(job_id: int):
    try:
        job = DatabaseManager().soft_delete_job(job_id)
    except Exception:
        logger.exception("Failed to dismiss job %s", job_id)
        raise HTTPException(status_code=500, detail="Failed to dismiss job")
    if job is None:
        raise HTTPException(status_code=404, detail="Open job not found")
    return {"job": job}


@router.post("/api/jobs/{job_id}/submissions", status_code=201)
@_serialized
def record_job_submission(job_id: int, payload: JobSubmissionRequest):
    try:
        submission = DatabaseManager().add_job_submission(
            job_id, **payload.model_dump()
        )
    except ValueError:
        raise
    except Exception:
        logger.exception("Failed to record submission for job %s", job_id)
        raise HTTPException(status_code=500, detail="Failed to record submission")
    if submission is None:
        raise HTTPException(status_code=404, detail="Open job not found")
    return {"submission": submission}


@router.get("/api/volunteer")
def list_volunteer_roles():
    """Volunteer page sections: current, applied, possible (+ stale possible)."""
    try:
        return DatabaseManager().get_volunteer_roles()
    except Exception:
        logger.exception("Failed to list volunteer roles")
        raise HTTPException(status_code=500, detail="Failed to list volunteer roles")


@router.post("/api/jobs/{job_id}/volunteering")
@_serialized
def start_volunteering(job_id: int):
    """Record that the user started volunteering in this role (idempotent)."""
    try:
        job = DatabaseManager().start_volunteering(job_id)
    except JobsMigrationPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        logger.exception("Failed to mark volunteering for job %s", job_id)
        raise HTTPException(status_code=500, detail="Failed to mark volunteering")
    if job is None:
        raise HTTPException(status_code=404, detail="Volunteer role not found")
    return {"job": job}


@router.post("/api/jobs/{job_id}/agent-prompt")
def queue_job_agent_prompt(request: Request, job_id: int):
    """Queue an agent prompt to tailor resume and cover letter for a job.
    
    Card #7400: Composes a prompt with job details and the house rules named
    by the local config's [jobs] tailor_rules (none when unset), then queues
    it via pt message send. The receiving agent drafts materials
    under review; this endpoint never sends anything to employers.
    """
    _require_local_admin_request(request)
    
    db = DatabaseManager()
    jobs = [j for j in db.get_open_jobs() if j["id"] == job_id]
    if not jobs:
        raise HTTPException(status_code=404, detail="Job not found or already submitted/dismissed")
    
    job = jobs[0]
    try:
        rules_path = tailor_rules()
        rules = None
        if rules_path is not None:
            with open(rules_path, encoding="utf-8") as handle:
                rules = handle.read().strip()
    except (ValueError, OSError, UnicodeDecodeError) as exc:  # the private config names a file it can't load
        raise HTTPException(status_code=503, detail=f"tailor rules can't be loaded: {exc}")

    prompt = f"""Tailor resume and cover letter for this job posting:

Company: {job['company']}
Title: {job['title']}
URL: {job['url']}

"""
    # A configured file is the rules, even an empty one; only no setting means none.
    if rules is not None:
        prompt += rules.replace("{company}", job["company"]) + "\n\n"
    prompt += "Review the posting at the URL above and create tailored materials" + (
        " following these rules." if rules is not None else ".")
    
    result = run_agent_command("pt", "message", "send " + shlex.quote(prompt))
    
    if not result.success:
        logger.error("Failed to queue agent prompt for job %s: %s", job_id, result.error)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to queue agent prompt: {result.error or 'Unknown error'}"
        )
    
    return {
        "success": True,
        "job_id": job_id,
        "message": "Agent prompt queued successfully"
    }
