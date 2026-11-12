"""The Codebase size API behind the dashboard's /codebase page (#8083)."""

import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status

from addons.codebase_size.db import CodebaseSizeDB
from dashboard.common import _own_lock, _require_local_admin_request, logger
from scripts.config import projects_root as config_projects_root

router = APIRouter()


# Overlapping refreshes are serialized: a second one while a scan runs gets
# 409 instead of a concurrent scan, so each refresh reports the run it stored.
_codebase_refresh_lock = threading.Lock()


def _codebase_report(db: CodebaseSizeDB) -> dict:
    """Report for the newest stored run against the baseline run.

    Both runs come from one read transaction (``codebase_runs``), so the
    response reflects one consistent snapshot. No runs yet is the valid empty
    state. A DB failure propagates: it must not read as "no runs".
    """
    from addons.codebase_size import scanner as cs

    base_date, base, latest_date, latest = db.codebase_runs()
    return cs.build_report(list(latest.values()), base, base_date, latest_date)


def _codebase_db_error(exc: Exception) -> HTTPException:
    logger.exception("codebase size request failed")
    if "no such table" in str(exc):
        detail = "Table codebase_size_snapshots is missing; run `pt db migrate`."
    else:
        detail = f"Codebase size data is unavailable: {exc}"
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


@router.get("/api/codebase-size")
def get_codebase_size():
    """Latest stored codebase size run with score vs the baseline. No scan."""
    try:
        return _codebase_report(CodebaseSizeDB())
    except Exception as exc:
        raise _codebase_db_error(exc)


@router.post("/api/codebase-size/refresh")
@_own_lock
def refresh_codebase_size(request: Request):
    """Scan the portfolio, store one run if every repo scanned, return the report.

    A plain ``def`` so the multi-second scan runs in the threadpool.
    """
    _require_local_admin_request(request)
    from addons.codebase_size import scanner as cs

    if not _codebase_refresh_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A codebase scan is already running; wait for it to finish.",
        )
    try:
        try:
            db = CodebaseSizeDB()
            db.codebase_baseline()  # table check before the scan
        except Exception as exc:
            raise _codebase_db_error(exc)

        try:
            rows = cs.scan_portfolio(config_projects_root())
        except cs.ProjectsRootMissing as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
        except Exception as exc:
            logger.exception("codebase size scan failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Codebase scan failed: {exc}",
            )
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No git repos found under the projects root.",
            )
        failed = [r for r in rows if r.error]
        if failed:
            listing = "; ".join(f"{r.project}: {r.error}" for r in failed)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Scan failed for {len(failed)} repo(s); nothing was stored. {listing}",
            )
        today = datetime.now().date().isoformat()
        try:
            db.save_codebase_scan(rows, today)
            # Report the run this request stored; the baseline comes from one
            # consistent read (it is this run when no run existed before).
            base_date, base, _, _ = db.codebase_runs()
        except Exception as exc:
            raise _codebase_db_error(exc)
        return cs.build_report(rows, base, base_date, today)
    finally:
        _codebase_refresh_lock.release()
