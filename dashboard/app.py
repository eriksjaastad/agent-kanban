"""FastAPI web dashboard for project tracker."""

import os
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic as _monotonic
from typing import Dict, List

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# Add parent directory to path for logger import
sys.path.insert(0, str(Path(__file__).parent.parent))

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.checkouts import MACHINE_LABELS
from db.manager import DatabaseManager
from discovery.alert_detector import get_all_alerts
from discovery.project_scanner import discover_projects

from dashboard.common import (  # noqa: E402
    _bulk_enrich,
    _serialized,
    build_navigation_payload,
    frontend_dist,
    logger,
    serve_spa_shell,
)

# Import config
from scripts.addon_registry import enabled as enabled_addons, load as load_addon_part
from scripts.config import (
    PROJECTS_BASE_DIR,
    owner_name,
    owner_prefix,
    user_facing_projects,
)
from scripts.pt import rebuild_project_graph
from scripts.utils.validation import (
    get_blocked_card_project_ids,
    get_blocked_card_reason,
    is_card_creation_allowed,
)
from dashboard.routes.github import (  # noqa: E402
    merged_prs_weekly,
    router as github_router,
)
from dashboard.routes.live_sites import (  # noqa: E402
    router as live_sites_router,
)
from dashboard.routes.review_health import (  # noqa: E402
    router as review_health_router,
)
from dashboard.routes.site_stats import (  # noqa: E402
    router as site_stats_router,
)
from dashboard.routes.ideas import (  # noqa: E402
    router as ideas_router,
)
from dashboard.routes.morning import (  # noqa: E402
    router as morning_router,
)
from dashboard.routes.outreach import (  # noqa: E402
    router as outreach_router,
)
from dashboard.routes.jobs import (  # noqa: E402
    router as jobs_router,
)
from dashboard.routes.agentic import (  # noqa: E402
    router as agentic_router,
)
from dashboard.routes.tasks import (  # noqa: E402
    _extract_field_from_error,
    _extract_pattern_from_error,
    router as tasks_router,
)
from dashboard.routes.memory import (  # noqa: E402
    _create_rebuild_job,
    _run_tracked_rebuild,
    router as memory_router,
)
from dashboard.routes.calendar import (  # noqa: E402
    router as calendar_router,
)

# The open columns of the Kanban board, in board order. Done and Cancelled are
# deliberately absent: these drive the dashboard breakdown of work still in
# flight, not an archive count.
BOARD_COLUMNS = ("Backlog", "To Do", "In Progress", "Review")

@asynccontextmanager
async def _lifespan(application: FastAPI):
    """Lifespan handler: rebuild project graph in background after server starts."""
    def _rebuild():
        try:
            from scripts.pt import rebuild_project_graph
            logger.info("Background graph rebuild starting...")
            rebuild_project_graph()
            logger.info("Background graph rebuild complete.")
        except Exception as e:
            logger.error(f"Background graph rebuild failed: {e}")

    thread = threading.Thread(target=_rebuild, daemon=True, name="graph-rebuild")
    thread.start()
    # Only the launchd-run server pings Discord; a test client or scratch server never does.
    if os.environ.get("PT_BLOCK_PINGS") == "1":
        from dashboard import block_ping
        block_ping.start()
    yield  # server is running
    # shutdown: nothing to clean up (daemon thread dies with process)


app = FastAPI(title="Project Tracker Dashboard", lifespan=_lifespan)


# Wall-clock and monotonic marks taken at import so /api/health can report how
# long this process has been up (a restarted dashboard looks identical to a
# healthy one without this).
_PROCESS_START_WALL = datetime.now()
_PROCESS_START_MONOTONIC = _monotonic()

# Database initialization happens when a manager is requested, never at import.

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/kanban", response_class=HTMLResponse)
@app.get("/kanban/{project}", response_class=HTMLResponse)
@app.get("/agentic", response_class=HTMLResponse)
@app.get("/calendar", response_class=HTMLResponse)
@app.get("/agent-chat", response_class=HTMLResponse)
@app.get("/jobs", response_class=HTMLResponse)
@app.get("/jobs/submitted", response_class=HTMLResponse)
@app.get("/jobs/volunteer", response_class=HTMLResponse)
@app.get("/morning", response_class=HTMLResponse)
async def serve_react_app(request: Request):
    """Serve the React frontend for SPA routes."""
    return await serve_spa_shell(request)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Redirect the root route to the canonical dashboard URL."""
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/api/agent-chat/messages")
def api_agent_chat_messages(limit: int = 100):
    """Read-only Agent Chat board — ALL traffic, newest first (#7145).

    Deliberately does not default to inbox filtering. The CLI trap of a
    complete-looking subset is exactly what this page exists to avoid.
    """
    import json as json_mod
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    limit = max(1, min(int(limit or 100), 500))
    # governance: allow-silent SF003: empty means unset; ~/.claude/agent-chat.env may fill it below, and an empty url or key is refused with 503
    config = {"url": os.environ.get("AGENT_CHAT_URL", ""), "key": os.environ.get("AGENT_CHAT_API_KEY", "")}
    env_file = Path.home() / ".claude" / "agent-chat.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k == "AGENT_CHAT_URL":
                config["url"] = v
            elif k == "AGENT_CHAT_API_KEY":
                config["key"] = v
    if not config["url"] or not config["key"]:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent Chat not configured (AGENT_CHAT_URL / AGENT_CHAT_API_KEY)",
        )

    params = urlencode({"limit": str(limit), "order": "desc"})
    url = config["url"].rstrip("/") + "/messages?" + params
    req = Request(url, headers={"X-API-Key": config["key"]}, method="GET")
    try:
        with urlopen(req, timeout=15) as resp:
            payload = json_mod.loads(resp.read())
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Agent Chat API error {exc.code}: {body}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Agent Chat API unreachable: {exc}") from exc

    messages = list(payload.get("messages") or [])
    # Newest first for the board (server desc returns newest page; reverse if ASC)
    if payload.get("order") != "desc":
        messages = list(reversed(messages))
    # Index by id for reply threading on the client
    by_id = {m.get("id"): m for m in messages if m.get("id") is not None}
    for m in messages:
        parent_id = m.get("reply_to")
        m["reply_to_message"] = by_id.get(parent_id) if parent_id else None
    return {"messages": messages, "count": len(messages), "scope": "all"}


@app.get("/api/navigation")
async def api_navigation():
    """Return shared top-level navigation metadata for all app shells."""
    return build_navigation_payload()


@app.post("/api/refresh")
@_serialized
def refresh_data():
    """Trigger full data refresh."""
    try:
        db = DatabaseManager()
        
        # Scan projects
        projects = discover_projects()
        
        # Update database
        for project in projects:
            outcome = db.upsert_scanned_project(project)
            if not outcome["ok"]:
                logger.error(
                    "refresh: failed to upsert project %s: %s",
                    outcome["project_id"],
                    outcome["error"],
                )

        # Clean up stale projects whose directories no longer exist on disk.
        # Only remove projects whose path is under the local PROJECTS_BASE_DIR
        # so we don't accidentally delete remote entries from other machines.
        disk_paths = {p["path"] for p in projects}
        local_prefix = str(PROJECTS_BASE_DIR)
        all_db_projects = db.get_all_projects()
        removed = 0
        for db_proj in all_db_projects:
            proj_path = db_proj.get("path", "")
            if (
                proj_path.startswith(local_prefix)
                and proj_path not in disk_paths
                and not Path(proj_path).exists()
            ):
                logger.info(f"Removing stale project from DB: {db_proj['id']} ({proj_path})")
                db.delete_project(db_proj["id"])
                removed += 1

        # Rebuild project graph in background under a tracked job so the
        # frontend can poll /api/rebuild-status/{job_id} for real completion.
        job_id = _create_rebuild_job("project_graph")
        return JSONResponse(
            {
                "status": "success",
                "message": f"Refreshed {len(projects)} projects, removed {removed} stale. Graph rebuild started in background.",
                "job_id": job_id,
            },
            background=BackgroundTask(_run_tracked_rebuild, job_id, rebuild_project_graph),
        )
    except Exception as e:
        logger.error(f"Error refreshing data: {e}")
        return JSONResponse({
            "status": "error",
            "message": str(e)
        }, status_code=500)


@app.get("/api/projects")
def api_projects():
    """JSON API for projects."""
    db = DatabaseManager()
    projects = db.get_all_projects(order_by="last_modified DESC")
    
    # Enrich with related data (bulk — 4 queries instead of 4N)
    enriched_projects = _bulk_enrich(projects, db)
    checkouts = db.get_open_checkouts()

    for project in enriched_projects:
        project["can_create_cards"] = is_card_creation_allowed(project["id"])
        project["blocked_card_reason"] = get_blocked_card_reason(project["id"])
        checkout = checkouts.get(project["id"])
        project["checkout"] = (
            {**checkout, "machine_label": MACHINE_LABELS[checkout["machine"]]} if checkout else None
        )

    return {"projects": enriched_projects}


@app.get("/api/task-policy")
def api_task_policy():
    """Return backend-owned task creation policy metadata for clients."""
    blocked_ids = get_blocked_card_project_ids()
    blocked_reasons = {project_id: get_blocked_card_reason(project_id) for project_id in blocked_ids}
    return {
        "blocked_project_ids": blocked_ids,
        "blocked_project_reasons": blocked_reasons,
    }


@app.get("/api/health")
def api_health():
    """Deep health check: is this process actually able to serve requests?

    Deliberately NOT a liveness stub. The dashboard has twice (2026-07-06,
    2026-07-08) gone "alive but broken" — the process stays up but its SQLite
    handle rots and every request 500s with "unable to open database file"
    (see #6482). A `{"status": "ok"}` constant would have reported green
    through both incidents and told the watchdog not to restart a dead server.

    So this opens its own DatabaseManager connection and runs a real query.
    Paying for a connection per call is the point, not overhead: it fails in
    the same way real requests fail.

    Not gated by _require_local_admin_request — health probes are
    conventionally unauthenticated, and a 403 to a remote monitor would be
    indistinguishable from a broken server. The payload is diagnostic only
    (row count, uptime); it exposes no task or project content.

    Returns 200 with the diagnostics when the DB answers, 503 with the error
    string when it does not — so both `curl` and the watchdog's %{http_code}
    check see the failure.
    """
    payload: Dict = {
        "status": "ok",
        "uptime_seconds": int(_monotonic() - _PROCESS_START_MONOTONIC),
        "started_at": _PROCESS_START_WALL.isoformat(timespec="seconds"),
    }

    try:
        snapshot = DatabaseManager().health_snapshot()
        payload["database"] = {
            "ok": True,
            "path": snapshot["path"],
            "task_count": snapshot["task_count"],
            "query_ms": snapshot["query_ms"],
        }
    except Exception as exc:
        logger.error(f"Health check failed: {exc}")
        payload["status"] = "error"
        payload["database"] = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        return JSONResponse(status_code=503, content=payload)

    return payload


@app.get("/api/alerts")
def api_alerts():
    """Get all alerts."""
    db = DatabaseManager()
    projects = db.get_all_projects()
    
    enriched_projects = _bulk_enrich(projects, db)
    alerts = get_all_alerts(enriched_projects)
    return {"alerts": alerts}


@app.get("/api/kanban/breakdown")
def api_kanban_breakdown():
    """Per-project card counts for the open Kanban columns, plus the open
    cards waiting on the owner (``waiting_on_owner``, oldest first, with age in
    days), and the owner's name and card prefix from the local config.

    Feeds the dashboard's board breakdown. Deliberately an aggregate rather
    than a client-side count over /api/tasks: that endpoint enriches and
    returns every one of the ~2,400 live rows, which is a lot of payload to
    produce four columns of integers.
    """
    try:
        db = DatabaseManager()
        rows = db.get_task_counts_by_project(statuses=list(BOARD_COLUMNS))
        # A card cannot be created for an unregistered project and deleting a
        # project takes its cards with it, so the id fallback should never
        # fire — it is here so a name lookup miss degrades to the id rather
        # than 500-ing the whole section.
        project_names = {
            project["id"]: project.get("name") or project["id"]
            for project in db.get_all_projects()
        }

        columns: Dict[str, List[Dict[str, object]]] = {column: [] for column in BOARD_COLUMNS}
        for row in rows:
            project_id = row["project_id"]
            columns[row["status"]].append({
                "project_id": project_id,
                "project": project_names.get(project_id, project_id),
                "count": row["count"],
            })

        # Ranked by card count, heaviest first; name breaks ties so the order
        # is stable between polls instead of following SQLite's grouping.
        for entries in columns.values():
            entries.sort(key=lambda entry: (-entry["count"], str(entry["project"]).lower()))

        owner = owner_name()
        today = datetime.now()
        waiting = [
            {**card, "project": project_names.get(card["project_id"], card["project_id"]),
             "age_days": (today - datetime.fromisoformat(card["created_at"])).days}
            for card in db.get_owner_cards(list(BOARD_COLUMNS), owner_prefix(owner))
        ]

        return {
            "statuses": list(BOARD_COLUMNS),
            "columns": columns,
            "owner": owner,
            "owner_prefix": owner_prefix(owner),
            "waiting_on_owner": waiting,
            "totals": {
                column: sum(entry["count"] for entry in columns[column])
                for column in BOARD_COLUMNS
            },
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as exc:
        logger.error(f"Error building Kanban breakdown: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build the Kanban breakdown"
        )


@app.get("/api/stats/user-facing-weekly")
def api_user_facing_weekly():
    """Done cards and merged PRs per week for the last 8 weeks (Monday start, this machine's
    clock), split into projects on the config's [dashboard] user_facing list and everything else.
    Merged PRs come from GitHub in the background (see merged_prs_weekly).

    Lots of activity, nobody using anything: this is the number that shows it.
    The list is read on every request, so editing it recalculates the split.
    """
    try:
        user_facing = user_facing_projects()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
    this_monday = datetime.now().date() - timedelta(days=datetime.now().weekday())
    starts = [this_monday - timedelta(weeks=n) for n in range(7, -1, -1)]
    counts = {start: {"user_facing": 0, "internal": 0} for start in starts}
    for card in DatabaseManager().get_done_since(starts[0].isoformat()):
        done = datetime.fromisoformat(card["completed_at"]).date()
        side = "user_facing" if card["project_id"] in user_facing else "internal"
        counts[done - timedelta(days=done.weekday())][side] += 1
    return {
        "user_facing_projects": sorted(user_facing),
        "weeks": [{"week_start": start.isoformat(), **counts[start]} for start in starts],
        "merged_prs": merged_prs_weekly(user_facing, starts),
    }


# --- Section ---
# Shadow Pricing API
# --- Section ---


@app.get("/api/shadow-pricing")
def shadow_pricing():
    """Shadow pricing: what Claude Max usage would cost at API rates."""
    from dashboard.shadow_pricing import get_shadow_pricing_data
    try:
        data = get_shadow_pricing_data()
        return JSONResponse(data)
    except FileNotFoundError as e:
        logger.warning(f"Shadow pricing file not found: {e}")
        return JSONResponse({"error": str(e), "data": {}}, status_code=404)
    except Exception as e:
        logger.warning(f"Shadow pricing error: {e}")
        return JSONResponse({"error": "Shadow pricing unavailable", "data": {}}, status_code=500)


# --- Section ---
# Calendar API
# --- Section ---

app.include_router(calendar_router)


    # event_date / event_time are validated by CalendarManager.add_event (shared
    # validator in db.backend_calendar_manager); its ValueError becomes a 422 below.


app.include_router(memory_router)


app.include_router(tasks_router)


# --- Error Handling Middleware ---

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Handle validation errors (ValueError from DatabaseManager)."""
    error_message = str(exc)
    
    # Check if it's a validation error (contains common validation keywords)
    if any(keyword in error_message.lower() for keyword in [
        "must be", "invalid", "does not exist", "contains potential secret",
        "required", "too long", "too short"
    ]):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "validation_error",
                "message": error_message,
                "details": {
                    "field": _extract_field_from_error(error_message),
                    "pattern": _extract_pattern_from_error(error_message)
                }
            }
        )
    
    # Check if it's a not found error
    if "not found" in error_message.lower():
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": "not_found",
                "message": error_message
            }
        )
    
    # Default to 400 for other ValueError cases
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": "validation_error",
            "message": error_message
        }
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle FastAPI request validation errors."""
    errors = exc.errors()
    first_error = errors[0] if errors else {}

    # Debug logging
    try:
        body = await request.body()
        logger.error(f"Validation error for {request.method} {request.url.path}: {body.decode()}")
        logger.error(f"Errors: {errors}")
    except (UnicodeDecodeError, RuntimeError) as e:
        logger.debug(f"Could not read request body for logging: {e}")

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": "validation_error",
            "message": f"Invalid request: {first_error.get('msg', 'Validation failed')}",
            "details": {
                "field": ".".join(str(loc) for loc in first_error.get("loc", [])),
                "type": first_error.get("type", "unknown")
            }
        }
    )


@app.exception_handler(sqlite3.Error)
async def database_error_handler(request: Request, exc: sqlite3.Error):
    """Handle database errors."""
    logger.error(f"Database error: {exc}", exc_info=True)
    
    error_type = type(exc).__name__
    operation = "unknown"
    
    # Try to extract operation from request path
    if "/tasks" in str(request.url.path):
        if request.method == "POST":
            operation = "INSERT"
        elif request.method == "PATCH":
            operation = "UPDATE"
        elif request.method == "DELETE":
            operation = "DELETE"
        elif request.method == "GET":
            operation = "SELECT"
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "database_error",
            "message": "Failed to process request due to database error",
            "details": {
                "operation": operation,
                "table": "tasks",
                "error_type": error_type
            }
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected errors."""
    logger.error(f"Unexpected error: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred"
        }
    )


app.include_router(agentic_router)


# --- Jobs API Endpoints (#7396) ---

app.include_router(jobs_router)


# --- Outreach contacts API Endpoints (#7641) ---

app.include_router(outreach_router)


# --- Morning warm-up API Endpoint (#7616) ---

app.include_router(morning_router)


# --- Ideas API Endpoints (Task #4583) ---

app.include_router(ideas_router)


# ---------------------------------------------------------------------------
# GitHub API  (#5373)
# ---------------------------------------------------------------------------


app.include_router(github_router)
app.include_router(live_sites_router)
app.include_router(review_health_router)
app.include_router(site_stats_router)


# Each add-on's API and the pages the React app shows for it.
for _addon in enabled_addons():
    if _addon.router:
        app.include_router(load_addon_part(_addon.router))
    for _page in _addon.pages:
        app.add_api_route(_page, serve_react_app, methods=["GET"], response_class=HTMLResponse)


