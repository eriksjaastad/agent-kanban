"""FastAPI web dashboard for project tracker."""

import sys
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Optional, List, Dict, Iterator
from datetime import datetime, timedelta, timezone
import os
import shutil
import subprocess
import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, HTTPException, status, UploadFile, File, Query
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask
from fastapi.exceptions import RequestValidationError
import re
import sqlite3
import json
import uuid
import shlex
from time import monotonic as _monotonic, time as _time

import numpy as np

# Add parent directory to path for logger import
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.logger import get_logger

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.attachment_paths import attachments_dir
from db.backend_manager import NotesConflictError
from db.checkouts import MACHINE_LABELS
from db.jobs import JobsMigrationPendingError
from db.manager import DatabaseManager
from db.outreach import ContactStateConflictError
from discovery.project_scanner import discover_projects
from discovery.alert_detector import get_all_alerts
from discovery.agent_registry import run_agent_command
from dashboard.brain_stats import BrainStatsUnavailable, bash_stats, tool_stats
from pydantic import BaseModel

# Import config
from scripts.config import (
    MEMORY_NOT_CONFIGURED,
    PROJECTS_BASE_DIR,
    MemoryIntegration,
    local_config_path,
    memory_integration,
    projects_root as config_projects_root,
    tailor_rules,
)


def _memory_store() -> MemoryIntegration | None:
    """The configured memory store for this request, or None when the integration is off.

    Read once per request from the private local config (scripts/config.py).
    A config that can't be loaded is a 500 naming the file, never "off".
    """
    try:
        return memory_integration()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


def _memory_off_message() -> str:
    return MEMORY_NOT_CONFIGURED.format(path=local_config_path())
from scripts.scheduled_jobs import scheduled_jobs
from scripts.utils.checklist import ChecklistLineError, toggle_checklist_line
from scripts.utils.validation import get_blocked_card_reason, get_blocked_card_project_ids, is_card_creation_allowed

from scripts.pt import rebuild_project_graph

logger = get_logger(__name__)

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
    yield  # server is running
    # shutdown: nothing to clean up (daemon thread dies with process)


app = FastAPI(title="Project Tracker Dashboard", lifespan=_lifespan)



# Write handlers run one at a time on one dedicated thread. Blocking handlers
# are plain ``def`` so the event loop stays free; on the loop they used to run
# one at a time, which made every write atomic against every other (a status
# transition checked then written, a load-modify-save file, a delete backup).
# A single-worker executor keeps that: queued writes wait in its queue, not in
# FastAPI's threadpool, so a burst of writes cannot starve the reads. Only the
# handler's own work runs there: the request body is parsed before it and
# background tasks run after it. GET handlers only read and stay concurrent.
_write_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pt-write")


def _serialized(fn):
    """Run a write handler on the single write thread."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_write_executor, functools.partial(fn, *args, **kwargs))

    setattr(wrapper, "serialized", True)
    return wrapper

# Wall-clock and monotonic marks taken at import so /api/health can report how
# long this process has been up (a restarted dashboard looks identical to a
# healthy one without this).
_PROCESS_START_WALL = datetime.now()
_PROCESS_START_MONOTONIC = _monotonic()

# Database initialization happens when a manager is requested, never at import.

# Setup templates and static files
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def _friendly_time(value: str) -> str:
    """Convert ISO timestamp to '3:14 PM  Mar 26' style."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Convert to local time
        local_dt = dt.astimezone()
        return local_dt.strftime("%-I:%M %p  %b %-d")
    except (ValueError, TypeError):
        return value


templates.env.filters["friendly_time"] = _friendly_time

# Mount React frontend build
frontend_dist = Path(__file__).parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="assets")

NAVIGATION_TITLE = "Project Tracker"
# Single source of truth for the shared navigation shell. Top-level order is
# significant: seven compact entries, four of them dropdown groups. Reminders
# stays a top-level leaf: the label on every page is the reminder (#8282).
# Each leaf keeps its original id/href/match_prefixes/navigation_type; only some labels
# changed when the leaves moved inside a group (#7637).
NAVIGATION_ITEMS = [
    {
        "id": "morning",
        "label": "Morning",
        "href": "/morning",
        "match_prefixes": ["/morning"],
        "navigation_type": "spa",
    },
    {
        "id": "dashboard",
        "label": "Dashboard",
        "href": "/dashboard",
        "match_prefixes": ["/dashboard", "/project"],
        "navigation_type": "spa",
    },
    {
        "id": "group-kanban",
        "label": "Kanban",
        "children": [
            {
                "id": "kanban",
                "label": "Board",
                "href": "/kanban",
                "match_prefixes": ["/kanban"],
                "navigation_type": "spa",
            },
            {
                "id": "calendar",
                "label": "Calendar",
                "href": "/calendar",
                "match_prefixes": ["/calendar"],
                "navigation_type": "spa",
            },
        ],
    },
    {
        "id": "group-jobs",
        "label": "Jobs",
        "children": [
            {
                "id": "jobs",
                "label": "Listings",
                "href": "/jobs",
                "match_prefixes": ["/jobs"],
                "navigation_type": "spa",
            },
            {
                "id": "jobs-submitted",
                "label": "Submissions",
                "href": "/jobs/submitted",
                "match_prefixes": ["/jobs/submitted"],
                "navigation_type": "spa",
            },
            {
                "id": "jobs-volunteer",
                "label": "Volunteer",
                "href": "/jobs/volunteer",
                "match_prefixes": ["/jobs/volunteer"],
                "navigation_type": "spa",
            },
        ],
    },
    {
        "id": "group-agents",
        "label": "Agents",
        "children": [
            {
                "id": "agent-chat",
                "label": "Chat",
                "href": "/agent-chat",
                "match_prefixes": ["/agent-chat"],
                "navigation_type": "spa",
            },
            {
                "id": "agentic",
                "label": "Autonomy",
                "href": "/agentic",
                "match_prefixes": ["/agentic"],
                "navigation_type": "spa",
            },
            {
                "id": "codebase",
                "label": "Codebase size",
                "href": "/codebase",
                "match_prefixes": ["/codebase"],
                "navigation_type": "spa",
            },
        ],
    },
    {
        "id": "group-memory",
        "label": "Memory",
        "children": [
            {
                "id": "memory",
                "label": "Memory",
                "href": "/memory",
                "match_prefixes": ["/memory"],
                "navigation_type": "document",
            },
            {
                "id": "graph",
                "label": "Graph",
                "href": "/graph",
                "match_prefixes": ["/graph"],
                "navigation_type": "document",
            },
        ],
    },
    {
        "id": "reminders",
        "label": "Reminders",
        "href": "/reminders",
        "match_prefixes": ["/reminders"],
        "navigation_type": "spa",
    },
]


def is_navigation_item_active(item: Dict[str, object], path: str) -> bool:
    """Return True when a navigation item should be active for the given path."""
    for prefix in item.get("match_prefixes", []):
        if not isinstance(prefix, str):
            continue
        if path == prefix or path.startswith(f"{prefix}/"):
            return True
    return False


def _longest_match_prefix_length(item: Dict[str, object], path: str) -> int:
    """Return the length of the longest matching prefix, or -1 on no match."""
    best = -1
    for prefix in item.get("match_prefixes", []):
        if not isinstance(prefix, str):
            continue
        if path == prefix or path.startswith(f"{prefix}/"):
            best = max(best, len(prefix))
    return best


def iter_navigation_leaves(items: List[Dict[str, object]]) -> Iterator[Dict[str, object]]:
    """Yield every leaf navigation item, descending into group children.

    Kept as a helper because the leaves now live under `children` keys on the
    four dropdown groups, while tests and other consumers still need to find a
    leaf by id (agent-chat, spa-shell bootstrap, ...).
    """
    for item in items:
        children = item.get("children")
        if children:
            yield from iter_navigation_leaves(children)
        else:
            yield item


def build_navigation(current_path: Optional[str] = None) -> List[Dict[str, object]]:
    """Build navigation metadata for templates and API responses.

    Leaves keep today's flat shape. A group is emitted as a single top-level
    item with `href`/`navigation_type` taken from its first child, the union of
    its children's match prefixes (in order), and a `children` list where every
    child carries its own `active` flag. The most specific matching child wins
    so /jobs/submitted highlights Submissions rather than both Listings and
    Submissions.
    """
    nav_items: List[Dict[str, object]] = []
    path = current_path or ""
    for item in NAVIGATION_ITEMS:
        children = item.get("children")
        if children:
            nav_item = dict(item)
            first_child = children[0]
            nav_item["href"] = first_child["href"]
            nav_item["navigation_type"] = first_child["navigation_type"]
            nav_item["match_prefixes"] = [
                prefix
                for child in children
                for prefix in child.get("match_prefixes", [])
                if isinstance(prefix, str)
            ]
            match_lengths = [
                _longest_match_prefix_length(child, path) if path else -1
                for child in children
            ]
            best_length = max(match_lengths)
            child_items = []
            for child, match_length in zip(children, match_lengths):
                child_item = dict(child)
                child_item["active"] = bool(
                    path and match_length >= 0 and match_length == best_length
                )
                child_items.append(child_item)
            nav_item["active"] = any(child_item["active"] for child_item in child_items)
            nav_item["children"] = child_items
            nav_items.append(nav_item)
        else:
            nav_item = dict(item)
            nav_item["active"] = bool(path and is_navigation_item_active(item, path))
            nav_items.append(nav_item)
    return nav_items


def build_navigation_payload(current_path: Optional[str] = None) -> Dict[str, object]:
    """Return the shared navigation payload for templates, the API, and the SPA shell."""
    return {
        "title": NAVIGATION_TITLE,
        "items": build_navigation(current_path),
    }


def build_template_context(request: Request, **context) -> Dict[str, object]:
    """Attach shared shell/navigation context to Jinja responses."""
    return {
        "request": request,
        "nav_title": NAVIGATION_TITLE,
        "nav_items": build_navigation_payload(request.url.path)["items"],
        **context,
    }


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _is_loopback_host(host: Optional[str]) -> bool:
    """Return True when the host is local to this machine."""
    return bool(host and host in _LOOPBACK_HOSTS)


def _require_local_admin_request(request: Request) -> None:
    """Restrict sensitive execution endpoints to loopback by default."""
    if os.getenv("PT_ALLOW_REMOTE_ADMIN") == "1":
        return

    client_host = request.client.host if request.client else None
    if _is_loopback_host(client_host):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This endpoint is restricted to local requests. Set PT_ALLOW_REMOTE_ADMIN=1 to override.",
    )


def build_spa_shell_html(index_html: str, current_path: str) -> str:
    """Inject backend-owned navigation metadata into the SPA shell."""
    payload = json.dumps(build_navigation_payload(current_path)).replace("<", "\\u003c")
    bootstrap_script = f"<script>window.__PT_NAVIGATION__ = {payload};</script>"

    if "</head>" in index_html:
        return index_html.replace("</head>", f"    {bootstrap_script}\n</head>", 1)

    return f"{bootstrap_script}\n{index_html}"


async def serve_spa_shell(request: Request):
    """Serve the React SPA shell for SPA-owned routes."""
    index_path = frontend_dist / "index.html"
    if index_path.exists():
        return HTMLResponse(content=build_spa_shell_html(index_path.read_text(), request.url.path), status_code=200)

    # Fallback when the frontend bundle is not built.
    return HTMLResponse(
        content="<h1>Frontend not built</h1><p>Run <code>npm run build</code> in dashboard/frontend/</p>",
        status_code=503,
    )


@app.get("/dashboard", response_class=HTMLResponse)
@app.get("/kanban", response_class=HTMLResponse)
@app.get("/kanban/{project}", response_class=HTMLResponse)
@app.get("/agentic", response_class=HTMLResponse)
@app.get("/calendar", response_class=HTMLResponse)
@app.get("/agent-chat", response_class=HTMLResponse)
@app.get("/jobs", response_class=HTMLResponse)
@app.get("/jobs/submitted", response_class=HTMLResponse)
@app.get("/jobs/volunteer", response_class=HTMLResponse)
@app.get("/codebase", response_class=HTMLResponse)
@app.get("/morning", response_class=HTMLResponse)
@app.get("/reminders", response_class=HTMLResponse)
async def serve_react_app(request: Request):
    """Serve the React frontend for SPA routes."""
    return await serve_spa_shell(request)


def format_time_ago(iso_date: str) -> str:
    """Convert ISO date to human-readable time ago."""
    try:
        date = datetime.fromisoformat(iso_date.replace('Z', '+00:00'))
        now = datetime.now(date.tzinfo) if date.tzinfo else datetime.now()
        diff = now - date
        
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        
        if days > 365:
            years = days // 365
            return f"{years}y ago"
        elif days > 30:
            months = days // 30
            return f"{months}mo ago"
        elif days > 0:
            return f"{days}d ago"
        elif hours > 0:
            return f"{hours}h ago"
        elif minutes > 0:
            return f"{minutes}m ago"
        else:
            return "just now"
    except Exception as e:
        logger.warning(f"Failed to format date '{iso_date}': {e}")
        return iso_date


def categorize_services(services):
    """Categorize services by type."""
    categories = {
        "backend": [],
        "hosting": [],
        "ai": [],
        "storage": [],
        "database": [],
        "notifications": [],
        "monitoring": [],
        "other": []
    }
    
    # Service name to category mapping
    service_types = {
        # Backend Infrastructure (services, APIs, background jobs)
        "railway": "backend",
        "heroku": "backend",
        "aws": "backend",
        
        # Web Hosting (static sites, web apps)
        "vercel": "hosting",
        "netlify": "hosting",
        "cloudflare pages": "hosting",
        "github pages": "hosting",
        
        # AI
        "openai": "ai",
        "anthropic": "ai",
        "claude": "ai",
        "google ai": "ai",
        "gemini": "ai",
        "xai": "ai",
        "grok": "ai",
        
        # Storage
        "cloudflare r2": "storage",
        "r2": "storage",
        "s3": "storage",
        "google drive": "storage",
        "dropbox": "storage",
        
        # Database
        "postgres": "database",
        "postgresql": "database",
        "mysql": "database",
        "mongodb": "database",
        "sqlite": "database",
        
        # Notifications
        "discord": "notifications",
        "slack": "notifications",
        "telegram": "notifications",
        
        # Monitoring
        "healthchecks.io": "monitoring",
        "healthchecks": "monitoring",
        "sentry": "monitoring",
        "datadog": "monitoring",
    }
    
    for service in services:
        name_lower = service["service_name"].lower()
        category = "other"
        
        # Find matching category
        for service_key, cat in service_types.items():
            if service_key in name_lower:
                category = cat
                break
        
        categories[category].append(service)
    
    # Remove empty categories
    return {k: v for k, v in categories.items() if v}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Redirect the root route to the canonical dashboard URL."""
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


def _group_by_project(rows: List[Dict], key: str = "project_id") -> Dict[str, List[Dict]]:
    """Group a flat list of row dicts by project_id for O(1) per-project lookup."""
    grouped: Dict[str, List[Dict]] = {}
    for row in rows:
        pid = row.get(key, "")
        grouped.setdefault(pid, []).append(row)
    return grouped


def _bulk_enrich(projects: List[dict], db: DatabaseManager) -> List[dict]:
    """Enrich all projects using bulk-fetched data (4 DB queries instead of 4N).

    Populates: ai_agents, cron_jobs, services, tasks,
    and time formatting fields.
    """
    all_agents = _group_by_project(db.get_ai_agents())
    all_crons = _group_by_project(db.get_cron_jobs())
    all_services = _group_by_project(db.get_services())
    all_tasks = _group_by_project(db.get_tasks())

    for project in projects:
        pid = project["id"]

        # AI agents
        agents = all_agents.get(pid, [])
        project["ai_agents"] = [a["agent_name"] for a in agents]

        # Cron jobs
        jobs = all_crons.get(pid, [])
        project["has_cron"] = len(jobs) > 0
        project["cron_jobs"] = jobs

        # Services
        services = all_services.get(pid, [])
        project["services"] = [s["service_name"] for s in services]
        project["service_details"] = services
        project["services_by_category"] = categorize_services(services)

        # Tasks
        tasks = all_tasks.get(pid, [])
        open_tasks = [t for t in tasks if t.get("status") != "Done"]
        project["task_count"] = len(open_tasks)
        project["total_tasks"] = len(tasks)
        project["backlog_count"] = len([t for t in tasks if t.get("status") == "Backlog"])
        project["todo_count"] = len([t for t in tasks if t.get("status") == "To Do"])
        project["in_progress_count"] = len([t for t in tasks if t.get("status") == "In Progress"])
        project["review_count"] = len([t for t in tasks if t.get("status") == "Review"])

        # Time formatting
        project["last_modified_human"] = format_time_ago(project.get("last_modified", ""))
        if project.get("index_updated_at"):
            project["index_updated_human"] = format_time_ago(project["index_updated_at"])

        # Agent config health
        project["version_status"] = "unmanaged"

    return projects


def _collect_task_display_ids(task_payload: dict, acc: set[int]) -> None:
    """Collect task ids referenced by a task payload, including nested relations."""
    task_id = task_payload.get("id")
    if isinstance(task_id, int):
        acc.add(task_id)

    parent_id = task_payload.get("parent_id")
    if isinstance(parent_id, int):
        acc.add(parent_id)

    for key in ("blocked_by_ids", "incomplete_blocking_ids"):
        for ref_id in task_payload.get(key, []) or []:
            if isinstance(ref_id, int):
                acc.add(ref_id)

    parent = task_payload.get("parent")
    if isinstance(parent, dict):
        _collect_task_display_ids(parent, acc)

    for nested_key in ("subtasks", "blocking_tasks"):
        for nested_task in task_payload.get(nested_key, []) or []:
            if isinstance(nested_task, dict):
                _collect_task_display_ids(nested_task, acc)


def _apply_task_display_ids(task_payload: dict, display_map: dict[int, int]) -> dict:
    """Attach human display ids to a task payload recursively.

    Also converts every raw Snowflake-scale id field (``id``, ``parent_id``,
    ``blocked_by_ids``, ``incomplete_blocking_ids``, ``unresolved_blocking_ids``)
    to a decimal string. pt_id (#6044) generates 63-bit ids that exceed
    ``Number.MAX_SAFE_INTEGER`` (2^53-1); a bare JSON number above that
    threshold is silently rounded by every browser's ``JSON.parse``, so
    every PATCH built from a parsed id 404s against the wrong row (#7824).
    Stringify only after the int-keyed ``display_map`` lookups above run —
    they need the exact int to find the right display id.
    """
    task_id = task_payload.get("id")
    if isinstance(task_id, int):
        task_payload["display_id"] = display_map.get(task_id, task_id)
        task_payload["id"] = str(task_id)

    parent_id = task_payload.get("parent_id")
    if isinstance(parent_id, int):
        task_payload["parent_display_id"] = display_map.get(parent_id, parent_id)
        task_payload["parent_id"] = str(parent_id)

    blocked_by_ids = task_payload.get("blocked_by_ids")
    if isinstance(blocked_by_ids, list):
        task_payload["blocked_by_display_ids"] = [
            display_map.get(ref_id, ref_id) for ref_id in blocked_by_ids if isinstance(ref_id, int)
        ]
        task_payload["blocked_by_ids"] = [
            str(ref_id) if isinstance(ref_id, int) else ref_id for ref_id in blocked_by_ids
        ]

    incomplete_ids = task_payload.get("incomplete_blocking_ids")
    if isinstance(incomplete_ids, list):
        task_payload["incomplete_blocking_display_ids"] = [
            display_map.get(ref_id, ref_id) for ref_id in incomplete_ids if isinstance(ref_id, int)
        ]
        task_payload["incomplete_blocking_ids"] = [
            str(ref_id) if isinstance(ref_id, int) else ref_id for ref_id in incomplete_ids
        ]

    unresolved_ids = task_payload.get("unresolved_blocking_ids")
    if isinstance(unresolved_ids, list):
        task_payload["unresolved_blocking_ids"] = [
            str(ref_id) if isinstance(ref_id, int) else ref_id for ref_id in unresolved_ids
        ]

    parent = task_payload.get("parent")
    if isinstance(parent, dict):
        task_payload["parent"] = _apply_task_display_ids(parent, display_map)

    for nested_key in ("subtasks", "blocking_tasks"):
        nested_tasks = task_payload.get(nested_key)
        if isinstance(nested_tasks, list):
            task_payload[nested_key] = [
                _apply_task_display_ids(nested_task, display_map)
                for nested_task in nested_tasks
                if isinstance(nested_task, dict)
            ]

    return task_payload


def _enrich_task_payloads_with_display_ids(tasks: list[dict], db: DatabaseManager) -> list[dict]:
    """Bulk-attach display ids to task payloads."""
    task_ids: set[int] = set()
    for task in tasks:
        _collect_task_display_ids(task, task_ids)
    # Explicit arguments retain the shared manager contract; callers normalize
    # snowflake dict keys, so coerce before lookup or every card falls back to
    # the 20-digit pk (what the Kanban board was showing after cutover).
    raw_map = db.get_task_display_id_map(task_ids=list(task_ids)) if task_ids else {}
    display_map = {int(k): int(v) for k, v in (raw_map or {}).items()}
    return [_apply_task_display_ids(task, display_map) for task in tasks]


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
    """Per-project card counts for the open Kanban columns.

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

        return {
            "statuses": list(BOARD_COLUMNS),
            "columns": columns,
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

    # event_date / event_time are validated by CalendarManager.add_event (shared
    # validator in db.backend_calendar_manager); its ValueError becomes a 422 below.


def _stringify_pt_ids(record: dict, keys: tuple = ("id",)) -> dict:
    """Stringify a record's pt_id (Snowflake-scale) id fields in place (#7826).

    ideas / calendar_events / linked task ids come from pt_next_id (#6044) and
    can exceed Number.MAX_SAFE_INTEGER; a bare JSON number would be rounded by
    the browser's JSON.parse, so edits would hit the wrong row (see #7824).
    """
    for key in keys:
        if isinstance(record.get(key), int):
            record[key] = str(record[key])
    return record


def _get_cal_manager():
    from db.calendar_manager import CalendarManager
    return CalendarManager()


@app.get("/api/calendar/events")
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


@app.post("/api/calendar/events")
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


@app.patch("/api/calendar/events/{event_id}/done")
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


@app.get("/api/calendar/crons")
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


@app.get("/api/scheduled-jobs")
def api_scheduled_jobs():
    """Every launchd and crontab job on this Mac, live, including switched-off ones (#8377).

    Read-only. Returns ``{"jobs", "errors"}``; a source that could not be read is
    in ``errors`` with 200, so the page shows what it could read and what it could not.
    """
    return scheduled_jobs(Path.home(), config_projects_root())


_graph_cache: Dict = {"data": None, "timestamp": 0, "params": None}
_GRAPH_CACHE_TTL = 300  # 5 minutes


def _invalidate_graph_cache() -> None:
    """Drop the /api/memory-graph in-memory cache so the next request recomputes."""
    _graph_cache["data"] = None
    _graph_cache["timestamp"] = 0
    _graph_cache["params"] = None


# ---- Rebuild job tracker -------------------------------------------------
# Tracks in-flight rebuild jobs so the frontend can poll real completion
# status instead of guessing via node-count diffs. Job dicts live for the
# lifetime of the process; they're small and self-expire on restart.
_rebuild_jobs: Dict[str, Dict] = {}


def _run_tracked_rebuild(job_id: str, fn) -> None:
    """Run a rebuild function with status tracking. Invalidates graph cache on success."""
    job = _rebuild_jobs.get(job_id)
    if job is None:
        return
    job["status"] = "running"
    job["started_at"] = datetime.now().isoformat()
    try:
        fn()
        job["status"] = "done"
        job["error"] = None
        _invalidate_graph_cache()
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


@app.get("/api/rebuild-status/{job_id}")
async def get_rebuild_status(job_id: str):
    """Poll status of a rebuild job. Returns 404 if unknown job."""
    job = _rebuild_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job_id"}, status_code=404)
    return job


@app.get("/graph", response_class=HTMLResponse)
async def graph_view(request: Request):
    """Render the graph visualization page."""
    return templates.TemplateResponse(request, "graph.html", build_template_context(request))


@app.get("/memory", response_class=HTMLResponse)
async def memory_view(request: Request):
    """Render the memory graph visualization page."""
    return templates.TemplateResponse(request, "memory.html", build_template_context(request))


@app.get("/api/memory-graph")
async def get_memory_graph_data(
    min_similarity: float = 0.3,   # Minimum similarity threshold for edges
    max_edges_per_node: int = 10,  # Limit edges per node to keep response size reasonable
    max_nodes: int = 1000          # Cap nodes to prevent N×N memory explosion
):
    """Return full memory graph JSON for client-side rendering.

    Reads the configured memory store's `thoughts` table (content, metadata,
    embedding_blob); used by memory.js (canvas) and memory_svg.js (SVG).

    Type filtering is intentionally done client-side so the browser can
    switch filters instantly without a network round-trip.

    Uses numpy vectorized cosine similarity for ~1600x speedup over
    pure Python loops on large graphs.
    """
    # The store is resolved before the cache, and is part of its key, so a
    # config change takes effect on the next request rather than in 5 minutes.
    store = _memory_store()
    if store is None:
        return JSONResponse({"error": _memory_off_message()}, status_code=404)
    brain_db_path = store.brain_db

    now = _time()
    cache_key = (str(brain_db_path), min_similarity, max_edges_per_node, max_nodes)
    if (_graph_cache["data"] is not None
            and now - _graph_cache["timestamp"] < _GRAPH_CACHE_TTL
            and _graph_cache["params"] == cache_key):
        return _graph_cache["data"]

    if not brain_db_path.exists():
        return JSONResponse({
            "error": f"Memory database not found. Expected at: {brain_db_path}"
        }, status_code=404)

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                # The memory store keeps embeddings only as little-endian float32
                # blobs (its #8035); the JSON `embedding` column is empty. A
                # value that is not a BLOB holding a whole float32 vector is
                # skipped here, before LIMIT, as the memory store's unpack_embedding
                # drops it.
                "SELECT id, content, embedding_blob, metadata, created_at, source_machine FROM thoughts"
                " WHERE typeof(embedding_blob) = 'blob'"
                " AND length(embedding_blob) > 0 AND length(embedding_blob) % 4 = 0"
                " ORDER BY created_at DESC LIMIT ?",
                (max_nodes,)
            ).fetchall()
            total_in_db = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]

        nodes = []
        embeddings = []

        for row in rows:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            embedding = np.frombuffer(row["embedding_blob"], dtype="<f4")
            thought_type_val = metadata.get("type", "observation")

            nodes.append({
                "id": str(row["id"]),
                "content": row["content"],
                "type": thought_type_val,
                "project": metadata.get("project", ""),
                "agent_family": metadata.get("agent_family", ""),
                "created_at": row["created_at"],
                "source_machine": row["source_machine"] or "",
                "size": 1
            })
            embeddings.append(embedding)

        # Vectorized cosine similarity via numpy
        edges = []
        connection_counts = {node["id"]: 0 for node in nodes}

        if embeddings:
            matrix = np.array(embeddings, dtype=np.float32)
            # Drop raw Python lists immediately — numpy has its own copy
            del embeddings
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            normed = matrix / norms
            del matrix, norms  # free before allocating N×N
            sim_matrix = normed @ normed.T
            del normed  # free N×D before iterating N×N
            np.fill_diagonal(sim_matrix, 0)

            # Edge extraction with threshold + top-k per node
            for i in range(len(nodes)):
                row_slice = sim_matrix[i, i+1:]
                mask = row_slice > min_similarity
                indices = np.where(mask)[0]
                values = row_slice[mask]
                if len(values) > max_edges_per_node:
                    top_k = np.argpartition(values, -max_edges_per_node)[-max_edges_per_node:]
                    indices = indices[top_k]
                    values = values[top_k]
                for idx, sim_val in zip(indices, values):
                    j = int(idx) + i + 1
                    edges.append({
                        "source": nodes[i]["id"],
                        "target": nodes[j]["id"],
                        "similarity": round(float(sim_val), 3)
                    })
                    connection_counts[nodes[i]["id"]] += 1
                    connection_counts[nodes[j]["id"]] += 1
            del sim_matrix

        for node in nodes:
            node["size"] = connection_counts[node["id"]]

        result = {
            "generated_at": datetime.now().isoformat(),
            "stats": {
                "total_thoughts": len(nodes),
                "total_in_db": total_in_db,
                "total_connections": len(edges),
                "avg_connections": round(sum(connection_counts.values()) / len(nodes), 2) if nodes else 0
            },
            "nodes": nodes,
            "edges": edges
        }

        _graph_cache["data"] = result
        _graph_cache["timestamp"] = _time()
        _graph_cache["params"] = cache_key

        return result

    except Exception as e:
        logger.error(f"Error reading memory database: {e}")
        return JSONResponse({"error": f"Error reading memory data: {e}"}, status_code=500)


def _parse_bounded_int_param(request: Request, name: str, default: int, lo: int, hi: int):
    """Parse an integer query param with a default, clamped to [lo, hi].

    Returns (value, error_message). A non-integer value yields an error
    message so the caller can return a 400 instead of silently falling back
    to the default. Out-of-range integer values are clamped into the
    documented range.
    """
    raw = request.query_params.get(name)
    if raw is None:
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"{name} must be an integer (got {raw!r})"
    return max(lo, min(hi, value)), None


# Final edge query shared by the shown-edge select and the available-edge
# count. Endpoints are remapped through _cluster_map (dust -> cluster) and
# then required to land inside _kept_ids (populated with either the shown
# node ids or the available node ids before the cap).
_KNOWLEDGE_EDGE_FROM = """
    FROM graph_edges e
    LEFT JOIN _cluster_map cm_s ON e.source_node_id = cm_s.id
    LEFT JOIN _cluster_map cm_t ON e.target_node_id = cm_t.id
    WHERE COALESCE(cm_s.cluster_id, e.source_node_id) IN (SELECT id FROM _kept_ids)
      AND COALESCE(cm_t.cluster_id, e.target_node_id) IN (SELECT id FROM _kept_ids)
      AND COALESCE(cm_s.cluster_id, e.source_node_id) != COALESCE(cm_t.cluster_id, e.target_node_id)
"""

_KNOWLEDGE_EDGE_SELECT = (
    "SELECT COALESCE(cm_s.cluster_id, e.source_node_id) AS source, "
    "COALESCE(cm_t.cluster_id, e.target_node_id) AS target, e.type, e.weight "
    + _KNOWLEDGE_EDGE_FROM
    + " ORDER BY e.weight DESC LIMIT ?"
)

_KNOWLEDGE_EDGE_COUNT = (
    "SELECT COUNT(*) FROM ("
    "SELECT COALESCE(cm_s.cluster_id, e.source_node_id) AS source, "
    "COALESCE(cm_t.cluster_id, e.target_node_id) AS target, e.type "
    + _KNOWLEDGE_EDGE_FROM
    + " GROUP BY 1, 2, 3)"
)

_KNOWLEDGE_DEGREE_SQL = """
    SELECT node_id, SUM(cnt) AS degree FROM (
        SELECT e.source_node_id AS node_id, COUNT(*) AS cnt
        FROM graph_edges e
        INNER JOIN _visible_ids s ON e.source_node_id = s.id
        INNER JOIN _visible_ids t ON e.target_node_id = t.id
        GROUP BY e.source_node_id
        UNION ALL
        SELECT e.target_node_id AS node_id, COUNT(*) AS cnt
        FROM graph_edges e
        INNER JOIN _visible_ids s ON e.source_node_id = s.id
        INNER JOIN _visible_ids t ON e.target_node_id = t.id
        GROUP BY e.target_node_id
    ) GROUP BY node_id
"""



@app.get("/api/memory/knowledge-graph")
async def get_knowledge_graph(request: Request):
    """Return the knowledge graph from brain.db (graph_nodes + graph_edges).

    Query params:
        min_mentions: Minimum mention_count to include a node (default: 1, show all).
                      Edges where either endpoint is filtered out are also removed.
        cluster:      "auto" (default) clusters dust nodes when total > 2000,
                      "off" disables clustering, "on" forces it.
        max_nodes:    Cap on returned nodes after clustering (default 1500,
                      clamped to 50..20000). Non-integer values are a 400.
        max_edges:    Cap on returned edges after node selection (default 5000,
                      clamped to 100..300000). Edges are ordered by weight DESC
                      so the strongest edges survive. Non-integer values are a 400.

    Stats: total_nodes / total_edges are the SHOWN counts (same as nodes_shown
    / edges_shown). total_nodes_available / total_edges_available describe the
    graph after min_mentions + clustering but before the max_nodes / max_edges
    caps, and truncated is True when the response omits part of that graph.
    """
    try:
        min_mentions = int(request.query_params.get("min_mentions", 1))
    except (TypeError, ValueError):
        return JSONResponse({"error": "min_mentions must be an integer"}, status_code=400)

    max_nodes, err = _parse_bounded_int_param(request, "max_nodes", 1500, 50, 20000)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    max_edges, err = _parse_bounded_int_param(request, "max_edges", 5000, 100, 300000)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    cluster_mode = request.query_params.get("cluster", "auto")
    store = _memory_store()
    if store is None:
        return JSONResponse({"error": _memory_off_message()}, status_code=404)
    brain_db_path = store.brain_db

    if not brain_db_path.exists():
        return JSONResponse({"error": "brain.db not found"}, status_code=404)

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row

            nodes = [dict(r) for r in conn.execute(
                "SELECT id, type, name, description, mention_count as size FROM graph_nodes WHERE mention_count >= ?",
                (min_mentions,)
            ).fetchall()]

            # Degree per visible node, computed in SQL so we never materialize
            # the full edge set just to cluster dust nodes.
            conn.execute("CREATE TEMP TABLE _visible_ids (id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO _visible_ids VALUES (?)", [(n["id"],) for n in nodes])
            degree_rows = conn.execute(_KNOWLEDGE_DEGREE_SQL).fetchall()
            degree = {row["node_id"]: row["degree"] for row in degree_rows}

            # Server-side clustering: aggregate low-value nodes into type-based clusters
            should_cluster = (
                cluster_mode == "on"
                or (cluster_mode == "auto" and len(nodes) > 2000)
            )
            if should_cluster:
                nodes, cluster_id_map = _cluster_dust_nodes(nodes, degree)
            else:
                cluster_id_map = {}

            total_nodes_available = len(nodes)
            # Keep the top max_nodes nodes by size (mention_count; cluster
            # nodes count by their size).
            nodes.sort(key=lambda n: n["size"], reverse=True)
            shown_nodes = nodes[:max_nodes]
            shown_ids = {n["id"] for n in shown_nodes}

            conn.execute("CREATE TEMP TABLE _cluster_map (id INTEGER PRIMARY KEY, cluster_id INTEGER)")
            conn.executemany("INSERT INTO _cluster_map VALUES (?, ?)", list(cluster_id_map.items()))
            conn.execute("CREATE TEMP TABLE _kept_ids (id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO _kept_ids VALUES (?)", [(nid,) for nid in shown_ids])

            edges = [dict(r) for r in conn.execute(_KNOWLEDGE_EDGE_SELECT, (max_edges,)).fetchall()]

            # Remapping dust endpoints onto a cluster can collapse distinct DB
            # edges into the same (source, target, type) triple; keep the first,
            # which is the strongest because the query orders by weight DESC.
            seen_edge_keys = set()
            deduped_edges = []
            for e in edges:
                key = (e["source"], e["target"], e["type"])
                if key in seen_edge_keys:
                    continue
                seen_edge_keys.add(key)
                deduped_edges.append(e)
            edges = deduped_edges

            # Count the available edges (same remap/dedup rules, before caps).
            conn.execute("DELETE FROM _kept_ids")
            conn.executemany("INSERT INTO _kept_ids VALUES (?)", [(n["id"],) for n in nodes])
            total_edges_available = conn.execute(_KNOWLEDGE_EDGE_COUNT).fetchone()[0]

        truncated = (len(shown_nodes) < total_nodes_available) or (len(edges) < total_edges_available)

        return {
            "nodes": shown_nodes,
            "edges": edges,
            "stats": {
                "total_nodes": len(shown_nodes),
                "total_edges": len(edges),
                "clustered": should_cluster,
                "total_nodes_available": total_nodes_available,
                "total_edges_available": total_edges_available,
                "nodes_shown": len(shown_nodes),
                "edges_shown": len(edges),
                "truncated": truncated,
            }
        }
    except Exception as e:
        logger.error(f"knowledge graph error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/memory/build-status")
async def get_memory_build_status():
    """Read-only status of the memory store's scheduled graph build.

    Reads the JSON receipt the build writes, at [memory] build_receipt under
    the store's root, and shows [memory] build_schedule. "unknown" when the
    integration or the receipt isn't configured, or the receipt is missing,
    unreadable or invalid. Never presented as fresh.
    """
    store = _memory_store()
    schedule = store.build_schedule if store else None
    receipt_path = store.root / store.build_receipt if store and store.build_receipt else None
    unknown = {
        "state": "unknown",
        "started_at": None,
        "finished_at": None,
        "exit_status": None,
        "invocation": None,
        "schedule": schedule,
    }
    try:
        if receipt_path is None or not receipt_path.is_file():
            return unknown
        with open(receipt_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return unknown
        exit_status = data.get("exit_status")
        if isinstance(exit_status, bool) or not isinstance(exit_status, int):
            return unknown
        return {
            "state": "ok" if exit_status == 0 else "failed",
            "started_at": data.get("started_at") if isinstance(data.get("started_at"), str) else None,
            "finished_at": data.get("finished_at") if isinstance(data.get("finished_at"), str) else None,
            "exit_status": exit_status,
            "invocation": data.get("invocation") if isinstance(data.get("invocation"), str) else None,
            "schedule": schedule,
        }
    except (OSError, ValueError):
        return unknown


def _cluster_dust_nodes(nodes: list[dict], degree: dict) -> tuple[list[dict], dict]:
    """Collapse low-signal nodes into per-type aggregate clusters.

    Dust = mention_count <= 2 AND degree <= 5. These get grouped by type
    into a single cluster node per type, reducing rendered node count.
    Returns (nodes_with_clusters, {dust_node_id: cluster_id}).
    """
    from collections import defaultdict

    dust_by_type = defaultdict(list)
    for n in nodes:
        if n["size"] <= 2 and degree.get(n["id"], 0) <= 5:
            dust_by_type[n["type"]].append(n)

    if not dust_by_type:
        return nodes, {}

    # Build cluster nodes (negative IDs to avoid collisions)
    cluster_id_map = {}  # dust node id -> cluster id
    cluster_nodes = []
    for i, (ntype, dust_nodes) in enumerate(dust_by_type.items()):
        if len(dust_nodes) < 3:
            continue  # not worth clustering 1-2 nodes
        cluster_id = -(i + 1)
        for dn in dust_nodes:
            cluster_id_map[dn["id"]] = cluster_id
        cluster_nodes.append({
            "id": cluster_id,
            "type": ntype,
            "name": f"{len(dust_nodes)} {ntype}s (low-ref)",
            "description": f"Cluster of {len(dust_nodes)} {ntype} nodes with low references",
            "size": len(dust_nodes),
            "is_cluster": True,
            "cluster_count": len(dust_nodes),
        })

    # Keep non-dust nodes + add cluster nodes
    kept_nodes = [n for n in nodes if n["id"] not in cluster_id_map]
    kept_nodes.extend(cluster_nodes)
    return kept_nodes, cluster_id_map


@app.get("/api/memory/types")
async def get_memory_types():
    """Return distinct thought types in the configured memory store's `thoughts` table.

    Used by memory.js and memory_svg.js for the legend and filter dropdowns.
    """
    import sqlite3

    store = _memory_store()
    brain_db_path = store.brain_db if store else None

    if brain_db_path is None or not brain_db_path.exists():
        return {"types": ["observation", "decision", "idea", "question"]}

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT DISTINCT json_extract(metadata, '$.type') AS thought_type FROM thoughts "
                "WHERE metadata IS NOT NULL ORDER BY thought_type"
            ).fetchall()

        types = sorted(set(
            row["thought_type"] for row in rows
            if row["thought_type"] is not None
        ))
        # Always ensure the canonical types are present
        for t in ("observation", "decision", "idea", "question"):
            if t not in types:
                types.insert(0, t)

        return {"types": types}

    except Exception as e:
        logger.error(f"Error reading memory types: {e}")
        return {"types": ["observation", "decision", "idea", "question"]}


@app.get("/api/memory/heatmap")
async def get_memory_heatmap():
    """Return thought density grouped by date and type for the heatmap view.

    Reads the configured memory store's `thoughts` table; used by
    memory_heatmap.js. Returns rows of { date, type, count } sorted chronologically.
    """
    import sqlite3

    store = _memory_store()
    if store is None:
        return {"rows": [], "error": _memory_off_message()}
    brain_db_path = store.brain_db

    if not brain_db_path.exists():
        return {"rows": [], "error": "Memory database not found"}

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT
                    date(created_at) AS day,
                    json_extract(metadata, '$.type') AS thought_type,
                    COUNT(*) AS count
                FROM thoughts
                WHERE metadata IS NOT NULL
                GROUP BY day, thought_type
                ORDER BY day ASC, thought_type ASC
            """).fetchall()

        return {
            "rows": [
                {
                    "date": row["day"],
                    "type": row["thought_type"] or "observation",
                    "count": row["count"]
                }
                for row in rows
            ]
        }

    except Exception as e:
        logger.error(f"Error reading memory heatmap: {e}")
        return JSONResponse({"error": f"Error reading heatmap data: {e}"}, status_code=500)


@app.get("/api/graph")
async def get_graph_data(
    project: Optional[str] = None,      # Filter to single project
    file_types: Optional[str] = None,   # Comma-separated: "py,ts,md"
    include_orphans: bool = True,       # Show orphaned nodes
    min_connections: int = 0            # Filter by connection count
):
    """Return graph JSON for D3.js visualization."""
    import json
    graph_path = Path(__file__).parent.parent / "data" / "graph.json"
    if not graph_path.exists():
        return JSONResponse({
            "error": "Graph not built. Run: python scripts/discovery/graph_builder.py"
        }, status_code=404)

    try:
        graph = json.loads(graph_path.read_text())
    except Exception as e:
        logger.error(f"Error reading graph.json: {e}")
        return JSONResponse({"error": f"Error reading graph data: {e}"}, status_code=500)

    # Apply filters
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    # Filter by project
    if project:
        nodes = [n for n in nodes if n.get("project") == project]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    # Filter by file types
    if file_types:
        allowed_types = [t.strip().lower() for t in file_types.split(',')]
        nodes = [n for n in nodes if n.get("type") in allowed_types]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    # Filter orphans
    if not include_orphans:
        nodes = [n for n in nodes if not n.get("is_orphan")]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    # Filter by min connections
    if min_connections > 0:
        nodes = [n for n in nodes if n.get("size", 0) >= min_connections]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    return {
        "generated_at": graph.get("generated_at"),
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "orphan_count": len([n for n in nodes if n.get("is_orphan")])
        },
        "nodes": nodes,
        "edges": edges
    }


# Pydantic models for task requests
class TaskCreateRequest(BaseModel):
    text: str
    project_id: str
    status: Optional[str] = "Backlog"
    priority: Optional[str] = None
    task_type: Optional[str] = None
    category: Optional[str] = None
    parent_id: Optional[int] = None  # Task #4645
    blocked_by: Optional[List[int]] = None  # Task #4579
    # Acceptance criteria, required (#7608): must contain at least one
    # "- [ ] <text>" checklist line — see validate_acceptance_criteria().
    notes: Optional[str] = None


class ChecklistToggleRequest(BaseModel):
    """Body for POST /api/tasks/{id}/checklist (#7821).

    ``base_notes`` is the exact notes text the client rendered the checkbox
    from. It is what identifies the line: an index plus item text cannot tell
    two identical items apart once another writer moves or removes one.
    """
    line_index: int
    expected_text: str
    checked: bool
    base_notes: Optional[str]


CHECKLIST_TOGGLE_MAX_ATTEMPTS = 3


class TaskUpdateRequest(BaseModel):
    text: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None
    commit_sha: Optional[str] = None
    category: Optional[str] = None
    review_comment: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    task_type: Optional[str] = None
    parent_id: Optional[int] = None  # Task #4645
    blocked_by: Optional[List[int]] = None  # Task #4579
    # The notes the edit started from. When sent with `notes`, the write only
    # applies if the stored notes still equal it; otherwise 409 (#7821).
    base_notes: Optional[str] = None


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


def _extract_field_from_error(error_message: str) -> Optional[str]:
    """Extract field name from error message."""
    # Common patterns: "Task text...", "Project ID...", "Status...", "Priority..."
    if "acceptance criteria" in error_message.lower():
        # #7608: create_card() rejects a missing/malformed checklist in
        # notes before add_task() ever runs.
        return "notes"
    elif "task text" in error_message.lower() or "text" in error_message.lower():
        return "text"
    elif "project" in error_message.lower():
        return "project_id"
    elif "status" in error_message.lower():
        return "status"
    elif "priority" in error_message.lower():
        return "priority"
    return None


def _extract_pattern_from_error(error_message: str) -> Optional[str]:
    """Extract secret pattern from error message if present."""
    if "pattern:" in error_message.lower():
        parts = error_message.split("pattern:")
        if len(parts) > 1:
            return parts[1].strip()
    return None


# --- Task API Endpoints ---

@app.post("/api/tasks", status_code=status.HTTP_201_CREATED)
@_serialized
def create_task(task_data: TaskCreateRequest):
    """Create a new task.

    Args:
        task_data: Task creation data (text, project_id, status, priority,
            notes). ``notes`` must include at least one "- [ ] <text>"
            acceptance-criteria checklist line (#7608) — enforced by
            ``DatabaseManager.create_card()``, not ``add_task()``. A missing
            or malformed checklist returns 400 with a message naming the
            exact format expected; see ``_extract_field_from_error``.

    Returns:
        Created task object with ID and timestamps

    Raises:
        HTTPException: 400 if validation fails, 404 if project not found
    """
    try:
        db = DatabaseManager()
        
        # Convert blocked_by list to JSON string if provided
        blocked_by_json = None
        if task_data.blocked_by:
            import json
            blocked_by_json = json.dumps(task_data.blocked_by)
        
        task = db.create_card(
            text=task_data.text,
            project_id=task_data.project_id,
            status=task_data.status or "Backlog",
            priority=task_data.priority,
            task_type=task_data.task_type,
            category=task_data.category,
            parent_id=task_data.parent_id,
            blocked_by=blocked_by_json,
            notes=task_data.notes,
        )
        return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error creating task: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create task"
        )


@app.get("/api/tasks")
def list_tasks(
    project_id: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    task_status: Optional[str] = None,
    include_subtasks: bool = True,
    parent_id: Optional[int] = None,
    include_archived: bool = False
):
    """List tasks with optional filtering.

    Args:
        project_id: Filter by project ID (optional)
        status_filter: Filter by status (optional; canonical query param)
        task_status: Legacy filter alias preserved for compatibility
        include_subtasks: Include subtasks in results (default: True)
        parent_id: Filter to subtasks of specific parent (optional)
        include_archived: Include Done cards past the per-project display
            cap (default: False, matching the board)

    Returns:
        Dictionary with enriched tasks list and total count
    """
    try:
        import json
        db = DatabaseManager()
        effective_status = status_filter if status_filter is not None else task_status
        tasks = db.get_tasks(
            project_id=project_id,
            status=effective_status,
            include_archived=include_archived,
        )

        subtasks_by_parent: Dict[int, List[Dict[str, object]]] = {}
        task_lookup = {task["id"]: dict(task) for task in tasks}

        # Blockers that are not in this fetch get resolved individually below.
        # `task_lookup` is scoped to one project's tasks and `blocked_by` is
        # not project-scoped at all, so a cross-project blocker — an intended
        # use case — misses on EVERY request, not just for archived or missing
        # ids. Resolving those uncached would be an N+1 against a sidebar that
        # calls this endpoint once per project. Cache per request, so each
        # distinct id costs at most one lookup no matter how many cards
        # reference it, and a card blocked by the same parent as its siblings
        # costs one query for all of them.
        blocker_cache: Dict[int, Optional[Dict[str, object]]] = {}

        def _resolve_blocker(blocked_id):
            if blocked_id in task_lookup:
                return task_lookup[blocked_id]
            if blocked_id not in blocker_cache:
                blocker_cache[blocked_id] = db.get_task(blocked_id)
            return blocker_cache[blocked_id]

        for task in tasks:
            parent = task.get("parent_id")
            if parent is not None:
                subtasks_by_parent.setdefault(parent, []).append(dict(task))

        # Filter by parent_id if specified
        if parent_id is not None:
            tasks = [t for t in tasks if t.get("parent_id") == parent_id]

        # Filter out subtasks if include_subtasks is False
        if not include_subtasks:
            tasks = [t for t in tasks if t.get("parent_id") is None]

        # Enrich each task with subtask info and blocking status
        # NOTE: Subtasks feature (#4645) is incomplete - methods not yet implemented
        enriched_tasks = []
        for task in tasks:
            task_dict = dict(task)

            # Build subtask metadata from the already-fetched task set so we
            # do not open a new SQLite connection for every row in the board.
            subtasks = subtasks_by_parent.get(task["id"], []) if include_subtasks else []
            if subtasks:
                ordered_subtasks = sorted(
                    subtasks,
                    key=lambda item: (
                        item.get("sequence_order") is None,
                        item.get("sequence_order") if item.get("sequence_order") is not None else 0,
                        item.get("created_at", ""),
                    ),
                )
                done_count = len([item for item in ordered_subtasks if item.get("status") == "Done"])
                task_dict["subtasks"] = ordered_subtasks
                task_dict["subtask_progress"] = {
                    "total": len(ordered_subtasks),
                    "done": done_count,
                    "percent": int(done_count / len(ordered_subtasks) * 100),
                }

            # Resolve blocking data from the same in-memory task set to avoid
            # N additional DB reads while rendering the Kanban board.
            if task.get("blocked_by"):
                try:
                    blocked_by_ids, blocked_by_error = db.parse_blocked_by(task)
                    if blocked_by_error:
                        # Fail closed (#6900): an unreadable blocked_by renders
                        # the card blocked, with the reason, not unblocked.
                        raise ValueError(blocked_by_error)
                    task_dict["blocked_by_ids"] = blocked_by_ids

                    # A miss in task_lookup used to be dropped silently, so a
                    # card whose blockers were not in the fetched set rendered
                    # as unblocked — the dashboard's version of the #6747 bug,
                    # where "(all resolved)" meant the opposite.
                    #
                    # A miss has two very different causes and they must not be
                    # conflated: the blocker may be archived (Done, and
                    # excluded by default since #6870 — genuinely not blocking),
                    # or it may not exist at all. Fall back to a PK lookup,
                    # which ignores archive state, and only then call it
                    # unresolvable.
                    blocking_tasks = []
                    unresolved_ids = []
                    for blocked_id in blocked_by_ids:
                        blocking_task = _resolve_blocker(blocked_id)
                        if blocking_task is None:
                            unresolved_ids.append(blocked_id)
                        else:
                            blocking_tasks.append(blocking_task)

                    incomplete_ids = [
                        blocking_task["id"]
                        for blocking_task in blocking_tasks
                        if blocking_task.get("status") != "Done"
                    ]
                    task_dict["blocking_tasks"] = blocking_tasks
                    # An id we cannot resolve is not proof of "not blocked".
                    # Fail towards blocked and name it, so a bad reference is
                    # visible rather than silently clearing the flag.
                    task_dict["is_blocked"] = bool(incomplete_ids or unresolved_ids)
                    task_dict["incomplete_blocking_ids"] = incomplete_ids
                    task_dict["unresolved_blocking_ids"] = unresolved_ids
                    if unresolved_ids:
                        logger.warning(
                            "Task %s references blocking ids that do not exist: %s",
                            task.get("id"), unresolved_ids,
                        )
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "Could not resolve blocked_by for task %s: %s",
                        task.get("id"),
                        exc,
                    )
                    task_dict.update(
                        blocked_by_ids=[],
                        blocking_tasks=[],
                        is_blocked=True,
                        incomplete_blocking_ids=[],
                        unresolved_blocking_ids=[],
                        blocked_by_error=str(exc),
                    )

            enriched_tasks.append(task_dict)

        enriched_tasks = _enrich_task_payloads_with_display_ids(enriched_tasks, db)
        return {
            "tasks": enriched_tasks,
            "total": len(enriched_tasks)
        }
    except Exception as e:
        logger.error(f"Error listing tasks: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve tasks"
        )


@app.get("/api/tasks/{task_id:int}")
def get_task(task_id: int):
    """Get a single task by ID with full enriched data.
    
    Args:
        task_id: Task ID
        
    Returns:
        Enriched task object with subtasks, parent, and blocking info
        
    Raises:
        HTTPException: 404 if task not found
    """
    try:
        import json
        db = DatabaseManager()
        task = db.get_task(task_id)
        
        if not task:
            raise ValueError(f"Task with ID {task_id} not found")
        
        task_dict = dict(task)
        
        # Add subtasks if this is a parent
        subtasks = db.get_subtasks(task_id)
        if subtasks:
            task_dict["subtasks"] = subtasks
            task_dict["subtask_progress"] = db.get_subtask_progress(task_id)
        
        # Add parent info if this is a subtask
        if task.get("parent_id"):
            parent = db.get_task(task["parent_id"])
            if parent:
                task_dict["parent"] = parent
        
        # Add blocking info if task has blocked_by
        # An unreadable blocked_by fails closed (#6900): the card is blocked
        # and the payload names why, instead of the fields silently vanishing.
        if task.get("blocked_by"):
            blocked_by_ids, blocked_by_error = db.parse_blocked_by(task)
            task_dict["blocked_by_ids"] = blocked_by_ids
            task_dict["blocking_tasks"] = db.get_blocking_tasks(task_id)
            is_blocked, blocking_ids = db.is_blocked(task_id)
            task_dict["is_blocked"] = is_blocked
            task_dict["incomplete_blocking_ids"] = blocking_ids
            if blocked_by_error:
                task_dict["blocked_by_error"] = blocked_by_error
        
        return _enrich_task_payloads_with_display_ids([task_dict], db)[0]
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error retrieving task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve task"
        )


@app.patch("/api/tasks/{task_id:int}")
@_serialized
def update_task(task_id: int, task_data: TaskUpdateRequest):
    """Update a task (text, status, priority).
    
    Args:
        task_id: Task ID
        task_data: Fields to update (text, status, priority)
        
    Returns:
        Updated task object
        
    Raises:
        HTTPException: 400 if validation fails, 404 if task not found, 409 if
            base_notes is sent with notes and no longer matches the stored notes
        
    Note:
        Status changes are automatically recorded in task_history. Notes
        changes are automatically recorded in task_notes_history.
    """
    try:
        db = DatabaseManager()
        provided_fields = getattr(task_data, "model_fields_set", None)
        if provided_fields is None:
            provided_fields = getattr(task_data, "__fields_set__", set())
        
        # Build update dict from non-None fields
        updates = {}
        if "text" in provided_fields:
            updates["text"] = task_data.text
        if "title" in provided_fields:
            updates["title"] = task_data.title
        if "notes" in provided_fields:
            updates["notes"] = task_data.notes
        if "commit_sha" in provided_fields:
            updates["commit_sha"] = task_data.commit_sha
        if "category" in provided_fields:
            updates["category"] = task_data.category
        if "review_comment" in provided_fields:
            updates["review_comment"] = task_data.review_comment
        if "status" in provided_fields:
            updates["status"] = task_data.status
        if "priority" in provided_fields:
            updates["priority"] = task_data.priority
        if "task_type" in provided_fields:
            updates["task_type"] = task_data.task_type
        if "parent_id" in provided_fields:
            updates["parent_id"] = task_data.parent_id
        if "blocked_by" in provided_fields:
            import json
            updates["blocked_by"] = (
                json.dumps(task_data.blocked_by) if task_data.blocked_by is not None else None
            )
        
        # If-Match: a stale base is answered before any validation, since the
        # edit's other values were chosen against notes that no longer hold.
        # update_task's `notes IS ?` still guards a write landing after this read.
        stale_notes = HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The notes changed since this edit started; nothing was saved.",
        )
        if "notes" in updates and "base_notes" in provided_fields:
            current = db.get_task(task_id)
            if current and current.get("notes") != task_data.base_notes:
                raise stale_notes
            updates["expected_notes"] = task_data.base_notes

        # Prompt check for In Progress — warn but don't block
        if updates.get("status") == "In Progress":
            current = db.get_task(task_id)
            if not current:
                raise ValueError(f"Task with ID {task_id} not found")
            prompt_value = updates.get("prompt")
            if prompt_value is None:
                prompt_value = current.get("prompt")
            task_type_value = updates.get("task_type")
            if task_type_value is None:
                task_type_value = current.get("task_type") or "manual"
            if task_type_value == "agent" and not prompt_value:
                logger.warning(f"Agent task #{task_id} started without a prompt")
            is_blocked, blocking_ids = db.is_blocked(task_id)
            if is_blocked and not blocking_ids:
                # Fail closed (#6900): an unreadable blocked_by blocks the start.
                reason = db.blocked_by_error(task_id) or "blocked_by unreadable"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot start task while blocked: {reason}"
                )
            if is_blocked:
                raw_blockers = db.get_task_display_id_map(task_ids=list(blocking_ids))
                blocking_display_map = {int(k): int(v) for k, v in (raw_blockers or {}).items()}
                blocking_str = ", ".join(
                    f"#{blocking_display_map.get(int(bid), bid)}" for bid in blocking_ids
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot start task while blocked by: {blocking_str}"
                )

        if not updates:
            # No updates provided, return current task
            task = db.get_task(task_id)
            if not task:
                raise ValueError(f"Task with ID {task_id} not found")
            return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
        
        # update_task handles history recording for status and notes changes
        try:
            task = db.update_task(task_id, notes_source="api", **updates)
        except NotesConflictError:
            raise stale_notes
        return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update task"
        )


@app.post("/api/tasks/{task_id:int}/checklist")
@_serialized
def toggle_task_checklist(task_id: int, body: ChecklistToggleRequest):
    """Tick or untick one notes checklist line as a server-side atomic toggle (#7821).

    The toggle applies only to the notes the client saw: if the stored notes
    differ from ``base_notes`` in any way, nothing is written and the answer
    is 409, so the client reloads and the user clicks again. Otherwise the
    server sets only the marker of ``line_index`` (which must be the
    checklist line whose item text is ``expected_text``) and writes with a
    compare-and-swap on the notes it read, so a writer that lands between
    the read and the write is never overwritten.

    404 when the task is missing, including one deleted while this request
    runs; 409 when the notes changed or the line is not the expected item.
    A toggle already in the requested state writes nothing and returns the
    task as read.
    """
    def not_found() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found",
        )

    def changed() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The notes changed since they were loaded; reload and try again",
        )

    try:
        db = DatabaseManager()
        for _ in range(CHECKLIST_TOGGLE_MAX_ATTEMPTS):
            current = db.get_task(task_id)
            if not current:
                raise not_found()
            old_notes = current.get("notes")
            if (old_notes or "") != (body.base_notes or ""):
                raise changed()
            try:
                new_notes = toggle_checklist_line(
                    old_notes, body.line_index, body.expected_text, body.checked
                )
            except ChecklistLineError as e:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
            if new_notes == old_notes:
                return _enrich_task_payloads_with_display_ids([dict(current)], db)[0]
            try:
                task = db.update_task(
                    task_id, notes_source="api", expected_notes=old_notes, notes=new_notes
                )
            except NotesConflictError:
                # Another write (or a delete) landed after our read. Re-read:
                # a changed note is a 409 above, a deleted task a 404.
                continue
            except ValueError:
                # update_task raises ValueError when the task vanished between
                # our read and its own.
                if not db.get_task(task_id):
                    raise not_found()
                raise
            if task is None:
                # Written, then deleted before update_task could re-read it.
                raise not_found()
            return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
        # Every attempt lost its compare-and-swap. Report what is true now.
        if not db.get_task(task_id):
            raise not_found()
        raise changed()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling checklist on task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle checklist item"
        )


@app.delete("/api/tasks/{task_id:int}", status_code=status.HTTP_200_OK)
@_serialized
def delete_task(task_id: int):
    """Delete a single task."""
    try:
        db = DatabaseManager()
        db.delete_task(task_id)
        # str(): task_id is a pt_id Snowflake int (#6044) that can exceed
        # Number.MAX_SAFE_INTEGER; a bare JSON number would round in the
        # browser (#7824).
        return {"deleted": True, "task_id": str(task_id)}
    except ValueError as e:
        message = str(e)
        if "SAFE_MODE=1" in message:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=message
            )
        if "not found" in message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message
        )
    except Exception as e:
        logger.error(f"Error deleting task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete task"
        )


# --- Attachment API Endpoints (#5216) ---

ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def _stringify_attachment_ids(record: dict) -> dict:
    """Stringify an attachment record's Snowflake-scale id fields (#7824).

    ``task_attachments.id`` and ``.task_id`` both come from pt_next_id
    (#6044) and can exceed Number.MAX_SAFE_INTEGER; left as bare JSON
    numbers they round in the browser the same way task ids did.
    """
    for key in ("id", "task_id"):
        if isinstance(record.get(key), int):
            record[key] = str(record[key])
    return record


@app.post("/api/tasks/{task_id:int}/attachments", status_code=status.HTTP_201_CREATED)
@_serialized
def upload_attachment(task_id: int, file: UploadFile = File(...)):
    """Upload a file and attach it to a task."""
    import mimetypes

    db = DatabaseManager()
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # The request parser has already spooled the upload; copy it in chunks so
    # we never hold >ATTACHMENT_MAX_BYTES in RAM.
    dest_dir = attachments_dir(task_id)
    ext = Path(file.filename or "upload").suffix
    stored_name = f"{uuid.uuid4()}{ext}"
    dest_path = dest_dir / stored_name

    size = 0
    try:
        with dest_path.open("wb") as out:
            while chunk := file.file.read(65_536):  # 64 KB chunks
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File exceeds 20 MB limit")
                out.write(chunk)
        mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
        record = db.add_attachment(
            task_id=task_id,
            filename=file.filename or stored_name,
            stored_name=stored_name,
            mime_type=mime_type,
            size_bytes=size,
        )
    except BaseException:
        # No row points at a partial, oversized or unrecorded file.
        dest_path.unlink(missing_ok=True)  # governance: allow-delete DS001: this request created the file moments ago and no attachment row references it
        raise
    return _stringify_attachment_ids(record)


@app.get("/api/tasks/{task_id:int}/attachments")
def list_attachments(task_id: int):
    """List all attachments for a task."""
    db = DatabaseManager()
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"attachments": [_stringify_attachment_ids(a) for a in db.get_attachments(task_id)]}


@app.delete("/api/tasks/{task_id:int}/attachments/{attachment_id}", status_code=status.HTTP_200_OK)
@_serialized
def delete_attachment(task_id: int, attachment_id: int):
    """Delete an attachment record and its file from disk."""
    db = DatabaseManager()
    record = db.delete_attachment(attachment_id=attachment_id, task_id=task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Attachment not found")
    file_path = attachments_dir(task_id, create=False) / record["stored_name"]
    if file_path.exists():
        # User-uploaded content goes to the Trash, recoverable. A misclick in
        # the UI should not destroy a file outright (#6905, #6899).
        from send2trash import send2trash
        try:
            send2trash(str(file_path))
        except OSError as exc:
            # The DB row is already gone and committed by this point, so raising
            # here would 500 the request while leaving the file orphaned on disk
            # and invisible to the UI — losing track of it rather than losing it.
            # Report the deletion that did happen and name the file loudly enough
            # that it can be reclaimed by hand.
            logger.error(
                "Attachment row %s deleted but its file could not be trashed; "
                "orphaned on disk at %s: %s",
                attachment_id, file_path, exc,
            )
            return {
                "deleted": True,
                "attachment_id": str(attachment_id),
                "file_orphaned": str(file_path),
            }
    return {"deleted": True, "attachment_id": str(attachment_id)}


@app.get("/api/attachments/{task_id}/{stored_name}")
def serve_attachment(task_id: int, stored_name: str):
    """Serve an attachment file for inline preview."""
    from fastapi.responses import FileResponse
    import mimetypes
    db = DatabaseManager()
    attachment = db.get_attachment(task_id, stored_name)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
    attach_dir = attachments_dir(task_id, create=False)
    file_path = (attach_dir / stored_name).resolve()
    # Path traversal guard: resolved path must stay inside the task's attachment dir
    if not file_path.is_relative_to(attach_dir.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(stored_name)[0] or "application/octet-stream"
    return FileResponse(path=str(file_path), media_type=media_type)


@app.get("/api/agentic/summary")
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
        }
    except Exception as e:
        logger.error(f"Error building agentic summary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to build agentic summary"
        )


# --- Codebase size API (#8083) ---


# Overlapping refreshes are serialized: a second one while a scan runs gets
# 409 instead of a concurrent scan, so each refresh reports the run it stored.
_codebase_refresh_lock = threading.Lock()


def _codebase_report(db: DatabaseManager) -> dict:
    """Report for the newest stored run against the baseline run.

    Both runs come from one read transaction (``codebase_runs``), so the
    response reflects one consistent snapshot. No runs yet is the valid empty
    state. A DB failure propagates: it must not read as "no runs".
    """
    from scripts import codebase_size as cs

    base_date, base, latest_date, latest = db.codebase_runs()
    return cs.build_report(list(latest.values()), base, base_date, latest_date)


def _codebase_db_error(exc: Exception) -> HTTPException:
    logger.exception("codebase size request failed")
    if "no such table" in str(exc):
        detail = "Table codebase_size_snapshots is missing; run `pt db migrate`."
    else:
        detail = f"Codebase size data is unavailable: {exc}"
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)


@app.get("/api/codebase-size")
def get_codebase_size():
    """Latest stored codebase size run with score vs the baseline. No scan."""
    try:
        return _codebase_report(DatabaseManager())
    except Exception as exc:
        raise _codebase_db_error(exc)


@app.post("/api/codebase-size/refresh")
def refresh_codebase_size(request: Request):
    """Scan the portfolio, store one run if every repo scanned, return the report.

    A plain ``def`` so the multi-second scan runs in the threadpool.
    """
    _require_local_admin_request(request)
    from scripts import codebase_size as cs

    if not _codebase_refresh_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A codebase scan is already running; wait for it to finish.",
        )
    try:
        try:
            db = DatabaseManager()
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


# --- Agentic Markers API (#5009) ---

MARKERS_PATH = Path(__file__).parent.parent / "data" / "agentic_markers.json"


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


@app.get("/api/agentic/markers")
def get_markers():
    """Return all agentic markers."""
    try:
        return {"markers": _load_markers()}
    except MarkersUnreadable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@app.post("/api/agentic/markers", status_code=201)
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


@app.patch("/api/agentic/markers/{marker_id}")
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


@app.delete("/api/agentic/markers/{marker_id}", status_code=204)
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


def _brain_stats(read, *args):
    """Run one dashboard.brain_stats reader; an unreadable brain.db is a 503 with the cause.

    The window ends today in UTC, the day the memory store stamps on each call.
    """
    store = _memory_store()
    if store is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_memory_off_message())
    brain_db = store.brain_db
    try:
        return read(brain_db, datetime.now(timezone.utc).date(), *args)
    except BrainStatsUnavailable as exc:
        logger.error("%s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@app.get("/api/tool-stats")
def get_tool_stats(tool: str = "WebSearch", days: int = 30, model: Optional[str] = None):
    """Calls of one tool per day for the /agentic chart (see brain_stats.tool_stats)."""
    return _brain_stats(tool_stats, tool, days, model)


@app.get("/api/bash-stats")
def get_bash_stats(days: int = 30):
    """Bash error rate per day for the /agentic chart (see brain_stats.bash_stats)."""
    return _brain_stats(bash_stats, days)


# --- Jobs API Endpoints (#7396) ---

class JobSubmissionRequest(BaseModel):
    submitted_at: Optional[str] = None
    resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/jobs")
def list_open_jobs():
    """Ranked open listings; ones older than 30 days come back separately."""
    try:
        ranked = DatabaseManager().get_ranked_open_jobs()
        return {"jobs": ranked["fresh"], "stale": ranked["stale"]}
    except Exception:
        logger.exception("Failed to list open jobs")
        raise HTTPException(status_code=500, detail="Failed to list jobs")


@app.get("/api/jobs/submissions")
def list_job_submissions():
    try:
        return {"jobs": DatabaseManager().get_submitted_jobs()}
    except Exception:
        logger.exception("Failed to list job submissions")
        raise HTTPException(status_code=500, detail="Failed to list submissions")


@app.get("/api/jobs/stats")
def job_stats():
    try:
        return DatabaseManager().get_job_stats()
    except Exception:
        logger.exception("Failed to read job stats")
        raise HTTPException(status_code=500, detail="Failed to read job stats")


@app.delete("/api/jobs/{job_id}")
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


@app.post("/api/jobs/{job_id}/submissions", status_code=201)
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


@app.get("/api/volunteer")
def list_volunteer_roles():
    """Volunteer page sections: current, applied, possible (+ stale possible)."""
    try:
        return DatabaseManager().get_volunteer_roles()
    except Exception:
        logger.exception("Failed to list volunteer roles")
        raise HTTPException(status_code=500, detail="Failed to list volunteer roles")


@app.post("/api/jobs/{job_id}/volunteering")
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


@app.post("/api/jobs/{job_id}/agent-prompt")
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


# --- Outreach contacts API Endpoints (#7641) ---

class OutreachContactRequest(BaseModel):
    name: str


@app.get("/api/outreach/contacts")
def list_outreach_contacts():
    try:
        contacts = DatabaseManager().list_active_contacts()
    except Exception:
        logger.exception("Failed to list outreach contacts")
        raise HTTPException(status_code=500, detail="Failed to list contacts")
    return {
        "contacts": [
            {
                "id": contact["id"],
                "name": contact["name"],
                "contacted_at": contact["contacted_at"],
                "created_at": contact["created_at"],
            }
            for contact in contacts
        ]
    }


@app.post("/api/outreach/contacts", status_code=201)
@_serialized
def add_outreach_contact(payload: OutreachContactRequest):
    try:
        contact = DatabaseManager().add_contact(payload.name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Name is required")
    except Exception:
        logger.exception("Failed to add outreach contact")
        raise HTTPException(status_code=500, detail="Failed to add contact")
    return {"contact": contact}


@app.patch("/api/outreach/contacts/{contact_id}")
@_serialized
def rename_outreach_contact(contact_id: int, payload: OutreachContactRequest):
    try:
        contact = DatabaseManager().rename_contact(contact_id, payload.name)
    except ContactStateConflictError:
        raise HTTPException(
            status_code=409, detail="Contact cannot be renamed after being replied"
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Name is required")
    except Exception:
        logger.exception("Failed to rename outreach contact %s", contact_id)
        raise HTTPException(status_code=500, detail="Failed to rename contact")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}


@app.post("/api/outreach/contacts/{contact_id}/contacted")
@_serialized
def mark_outreach_contacted(contact_id: int):
    try:
        contact = DatabaseManager().mark_contacted(contact_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="Contact cannot be marked contacted")
    except Exception:
        logger.exception("Failed to mark outreach contact %s as contacted", contact_id)
        raise HTTPException(status_code=500, detail="Failed to mark contacted")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}


@app.post("/api/outreach/contacts/{contact_id}/replied")
@_serialized
def mark_outreach_replied(contact_id: int):
    try:
        contact = DatabaseManager().mark_replied(contact_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="Contact cannot be marked replied")
    except Exception:
        logger.exception("Failed to mark outreach contact %s as replied", contact_id)
        raise HTTPException(status_code=500, detail="Failed to mark replied")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}


@app.delete("/api/outreach/contacts/{contact_id}")
@_serialized
def soft_delete_outreach_contact(contact_id: int):
    try:
        contact = DatabaseManager().soft_delete_contact(contact_id)
    except Exception:
        logger.exception("Failed to delete outreach contact %s", contact_id)
        raise HTTPException(status_code=500, detail="Failed to delete contact")
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"contact": contact}


# --- Reminders API Endpoints (#8282) ---

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


@app.get("/api/reminders")
def list_reminders():
    """Active reminders in the order added, then built-in ones newest first."""
    try:
        return DatabaseManager().list_reminders()
    except Exception as exc:
        raise _reminders_db_error("list reminders", exc)


@app.post("/api/reminders", status_code=201)
@_serialized
def add_reminder(payload: ReminderRequest):
    try:
        reminder = DatabaseManager().add_reminder(
            payload.name, payload.purpose, payload.location
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Name is required")
    except Exception as exc:
        raise _reminders_db_error("add reminder", exc)
    return {"reminder": reminder}


@app.post("/api/reminders/{reminder_id}/built-in")
@_serialized
def mark_reminder_built_in(reminder_id: int):
    try:
        reminder = DatabaseManager().mark_reminder_built_in(reminder_id)
    except Exception as exc:
        raise _reminders_db_error("mark reminder built in", exc)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"reminder": reminder}


@app.delete("/api/reminders/{reminder_id}")
@_serialized
def remove_reminder(reminder_id: int):
    try:
        reminder = DatabaseManager().remove_reminder(reminder_id)
    except Exception as exc:
        raise _reminders_db_error("remove reminder", exc)
    if reminder is None:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"reminder": reminder}


# --- Morning warm-up API Endpoint (#7616) ---

@app.get("/api/morning")
def get_morning_snapshot():
    try:
        from dashboard.morning import morning_snapshot, MorningPlanError
        return morning_snapshot()
    except MorningPlanError as exc:
        logger.error("Failed to load morning plan: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))


# --- Ideas API Endpoints (Task #4583) ---

class IdeaCreateRequest(BaseModel):
    text: str


class IdeaUpdateRequest(BaseModel):
    text: str


@app.get("/api/ideas")
def get_ideas():
    """Get all ideas.
    
    Returns:
        List of all ideas ordered by creation date (newest first)
    """
    try:
        db = DatabaseManager()
        ideas = db.get_all_ideas()
        return {"ideas": [_stringify_pt_ids(i) for i in ideas]}
    except Exception as e:
        logger.error(f"Error fetching ideas: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch ideas"
        )


@app.post("/api/ideas", status_code=status.HTTP_201_CREATED)
@_serialized
def create_idea(idea_data: IdeaCreateRequest):
    """Create a new idea.
    
    Args:
        idea_data: Idea creation data (text)
        
    Returns:
        Created idea with id and timestamps
        
    Raises:
        HTTPException: 400 if validation fails
    """
    try:
        db = DatabaseManager()
        idea = db.add_idea(idea_data.text)
        return _stringify_pt_ids(idea)
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error creating idea: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create idea"
        )


@app.patch("/api/ideas/{idea_id}")
@_serialized
def update_idea(idea_id: int, idea_data: IdeaUpdateRequest):
    """Update an idea's text.
    
    Args:
        idea_id: Idea ID
        idea_data: Updated text
        
    Returns:
        Updated idea
        
    Raises:
        HTTPException: 400 if validation fails, 404 if idea not found
    """
    try:
        db = DatabaseManager()
        idea = db.update_idea(idea_id, idea_data.text)
        return _stringify_pt_ids(idea)
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error updating idea {idea_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update idea"
        )


@app.delete("/api/ideas/{idea_id}", status_code=status.HTTP_200_OK)
@_serialized
def delete_idea(idea_id: int):
    """Delete an idea.
    
    Args:
        idea_id: Idea ID
        
    Returns:
        Success confirmation
        
    Raises:
        HTTPException: 404 if idea not found
    """
    try:
        db = DatabaseManager()
        db.delete_idea(idea_id)
        return {
            "success": True,
            "message": f"Idea {idea_id} deleted successfully"
        }
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error deleting idea {idea_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete idea"
        )


# ---------------------------------------------------------------------------
# GitHub API  (#5373)
# ---------------------------------------------------------------------------

from dashboard.github_cache import GitHubCache

_github_cache = GitHubCache()


def _find_gh() -> str:
    """Find gh binary, checking common install locations if not on PATH.

    launchd services have a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin),
    so shutil.which() alone won't find Homebrew-installed gh.
    """
    found = shutil.which("gh") or os.environ.get("GH_PATH")
    if found:
        return found
    # Check standard install locations (launchd PATH doesn't include these)
    homebrew_paths = [
        os.path.expanduser("~/.local/bin/gh"),
        os.path.join(os.environ.get("HOMEBREW_PREFIX", "/opt/homebrew"), "bin", "gh"),
    ]
    for candidate in homebrew_paths:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "gh"


_GH_BIN = _find_gh()

# Fields requested from `gh pr list --json`. Every name here must appear in
# GH_PR_LIST_VALID_FIELDS — `gh` rejects the whole call on one bad field, which
# silently emptied the dashboard's PR panel until #6749.
PR_LIST_FIELDS = (
    "title",
    "number",
    "url",
    "author",
    "createdAt",
    "statusCheckRollup",
    "reviewDecision",
    "headRefName",
    "baseRefName",
    "isDraft",
)

# Valid `gh pr list --json` field names, as reported by gh 2.96.0. Note this is
# NOT the `gh search prs` field set — that one has `repository`, this one does
# not, because `pr list` is already scoped to a single repo.
GH_PR_LIST_VALID_FIELDS = frozenset({
    "additions", "assignees", "author", "autoMergeRequest", "baseRefName",
    "baseRefOid", "body", "changedFiles", "closed", "closedAt",
    "closingIssuesReferences", "comments", "commits", "createdAt", "deletions",
    "files", "fullDatabaseId", "headRefName", "headRefOid", "headRepository",
    "headRepositoryOwner", "id", "isCrossRepository", "isDraft", "labels",
    "latestReviews", "maintainerCanModify", "mergeCommit", "mergeStateStatus",
    "mergeable", "mergedAt", "mergedBy", "milestone", "number",
    "potentialMergeCommit", "projectCards", "projectItems", "reactionGroups",
    "reviewDecision", "reviewRequests", "reviews", "state", "statusCheckRollup",
    "title", "updatedAt", "url",
})


# gh signals "this repo does not exist" differently per transport: the GraphQL
# path (`gh repo view`) and the REST path (`gh api`). Anything else is a real
# failure — auth lapse, rate limit, network — and must not be reported as
# "not on GitHub".
_GH_NOT_FOUND_MARKERS = (
    "could not resolve to a repository",
    "http 404",
)

# _gh_call failure kinds.
GH_OK = None
GH_NOT_FOUND = "not_found"
GH_ERROR = "error"


# Fields requested from `gh repo view --json`. Same all-or-nothing contract as
# PR_LIST_FIELDS — and a different valid set, since field sets are per-subcommand.
REPO_VIEW_FIELDS = (
    "name",
    "url",
    "pushedAt",
    "defaultBranchRef",
    "isPrivate",
    "description",
    "stargazerCount",
    "forkCount",
    "isArchived",
)

# Valid `gh repo view --json` field names, as reported by gh 2.96.0.
GH_REPO_VIEW_VALID_FIELDS = frozenset({
    "archivedAt", "assignableUsers", "codeOfConduct", "contactLinks", "createdAt",
    "defaultBranchRef", "deleteBranchOnMerge", "description", "diskUsage",
    "forkCount", "fundingLinks", "hasDiscussionsEnabled", "hasIssuesEnabled",
    "hasProjectsEnabled", "hasWikiEnabled", "homepageUrl", "id", "isArchived",
    "isBlankIssuesEnabled", "isEmpty", "isFork", "isInOrganization", "isMirror",
    "isPrivate", "isSecurityPolicyEnabled", "isTemplate",
    "isUserConfigurationRepository", "issueTemplates", "issues", "labels",
    "languages", "latestRelease", "licenseInfo", "mentionableUsers",
    "mergeCommitAllowed", "milestones", "mirrorUrl", "name", "nameWithOwner",
    "openGraphImageUrl", "owner", "parent", "primaryLanguage", "projects",
    "projectsV2", "pullRequestTemplates", "pullRequests", "pushedAt",
    "rebaseMergeAllowed", "repositoryTopics", "securityPolicyUrl",
    "squashMergeAllowed", "sshUrl", "stargazerCount", "templateRepository",
    "updatedAt", "url", "usesCustomOpenGraphImage", "viewerCanAdminister",
    "viewerDefaultCommitEmail", "viewerDefaultMergeMethod", "viewerHasStarred",
    "viewerPermission", "viewerPossibleCommitEmails", "viewerSubscription",
    "visibility", "watchers",
})


def _gh_call(args: List[str], timeout: int = 30, missing_ok: bool = False) -> tuple:
    """Run a gh CLI command, returning (parsed_json, failure_kind).

    failure_kind is GH_OK on success, GH_NOT_FOUND when the target genuinely
    does not exist, or GH_ERROR for any other failure. Callers need the
    distinction: a missing repo is a fact about the world, while an auth or
    rate-limit failure means our data is incomplete and we must say so.

    missing_ok is for a caller that expects some targets not to exist and
    counts them itself (the repo view of every tracked project): its
    not-found is logged at debug, not as a warning. Every other failure warns.

    stderr is logged but never returned — it can contain tokens and auth URLs,
    and callers surface these values to the browser.
    """
    try:
        result = subprocess.run(
            [_GH_BIN] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            blob = (stderr + " " + result.stdout).lower()
            not_found = any(marker in blob for marker in _GH_NOT_FOUND_MARKERS)
            log = logger.debug if (not_found and missing_ok) else logger.warning
            log(f"gh command failed: gh {' '.join(args)} — {stderr}")
            return None, (GH_NOT_FOUND if not_found else GH_ERROR)
        if not result.stdout.strip():
            return None, GH_OK
        return json.loads(result.stdout), GH_OK
    except FileNotFoundError:
        logger.error("gh CLI not found on PATH")
        return None, GH_ERROR
    except subprocess.TimeoutExpired:
        logger.warning(f"gh command timed out: gh {' '.join(args)}")
        return None, GH_ERROR
    except json.JSONDecodeError as e:
        logger.warning(f"gh returned non-JSON: {e}")
        return None, GH_ERROR


def _gh_json(args: List[str], timeout: int = 30) -> any:
    """Run a gh CLI command and return parsed JSON, or None on failure.

    Thin wrapper over _gh_call for callers that do not need to distinguish a
    missing target from a failed fetch.
    """
    return _gh_call(args, timeout)[0]


def _get_tracked_repo_names() -> List[str]:
    """Get repo names from the kanban board's project list.

    Includes top-level projects and subdirectories of github-repos/.
    Excludes internal names: anything starting with an underscore.

    A database failure raises: an empty list would read as "no tracked
    projects", and the GitHub cache already turns a raised refresh into
    refresh_error while keeping the previous snapshot.
    """
    db = DatabaseManager()
    projects = db.get_all_projects()
    names: List[str] = []
    for p in projects:
        pid = p.get("id", "")
        # Skip internal/infrastructure projects
        if pid.startswith("_") or pid.startswith("__"):
            continue
        # github-repos is a container — its children are the actual repos
        if pid == "github-repos":
            github_repos_dir = config_projects_root() / "github-repos"
            if github_repos_dir.is_dir():
                for child in github_repos_dir.iterdir():
                    if child.is_dir() and not child.name.startswith(".") and not child.name.startswith("00_"):
                        names.append(child.name)
            continue
        names.append(pid)
    return sorted(set(names))


# gh calls run a few at a time; GitHub's secondary rate limit allows far more.
_GH_WORKERS = 6


def _gh_map(fn, items: List) -> List:
    """Run fn over items on a few threads; results come back in input order."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=_GH_WORKERS, thread_name_prefix="gh") as pool:
        return list(pool.map(fn, items))


def _fetch_github_data() -> Dict:
    """Fetch GitHub data for tracked projects only (from kanban board).

    Each stage's calls run in parallel, and the snapshot is assembled in the
    same order a one-at-a-time fetch would produce.
    """

    # 1. Account info
    user = _gh_json(["api", "/user"])
    if user is None:
        return {"error": "gh CLI not available or not authenticated", "cached": False}

    owner = user.get("login", "")

    # 2. Get repo list from kanban board, then fetch details from GitHub
    tracked_names = _get_tracked_repo_names()
    if not tracked_names:
        return {"error": "No tracked projects found in kanban board", "cached": False}

    fetch_errors: List[str] = []

    repos: List[Dict] = []
    repos_missing = 0
    # About half the tracked projects aren't on GitHub (third-party checkouts in
    # github-repos/, local-only projects), so their not-found is expected and
    # counted below rather than logged as a warning every refresh.
    views = _gh_map(lambda name: _gh_call([
        "repo", "view", f"{owner}/{name}",
        "--json", ",".join(REPO_VIEW_FIELDS),
    ], timeout=10, missing_ok=True), tracked_names)
    for name, (repo_info, failure) in zip(tracked_names, views):
        if repo_info:
            repos.append(repo_info)
        elif failure == GH_NOT_FOUND:
            # CAVEAT: GitHub answers "no such repo" and "you cannot see this
            # repo" identically, by design, so it never leaks a private repo's
            # existence. A token that loses `repo` scope therefore makes every
            # private repo look absent rather than unreachable, and gh gives us
            # nothing to tell the two apart.
            #
            # We cannot resolve that here — but we refuse to let it be silent:
            # this count is surfaced in the dashboard summary, so losing access
            # shows up as a jump in "Not on GitHub" rather than as nothing.
            repos_missing += 1
            logger.debug(f"Tracked project '{name}' is not on GitHub (or not visible to this token)")
        else:
            # A failed fetch drops this repo from `repos` entirely, taking its
            # PRs, CI and branches with it. Counting that as "not on GitHub"
            # would assert a healthy fetch that never happened.
            fetch_errors.append(f"repo metadata for {owner}/{name}")

    # One line per refresh keeps a jump visible in the log (a token losing
    # `repo` scope makes private repos look absent; see the CAVEAT above).
    logger.info(f"GitHub: {repos_missing} of {len(tracked_names)} tracked projects not on GitHub")

    # 3. Open PRs across tracked repos
    all_prs: List[Dict] = []
    active = [r for r in repos if not r.get("isArchived")]
    pr_lists = _gh_map(lambda repo: _gh_call([
        "pr", "list",
        "--repo", f"{owner}/{repo.get('name', '')}",
        "--json", ",".join(PR_LIST_FIELDS),
        "--state", "open",
        "--limit", "50",
    ], timeout=15), active)
    for repo, (prs, failure) in zip(active, pr_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            # Distinguish a failed fetch from a repo with no open PRs. Without
            # this the UI renders an empty PR panel either way, which is how a
            # bad --json field went unnoticed (see #6749).
            fetch_errors.append(f"open PRs for {owner}/{repo_name}")
            continue
        # `gh pr list` is already scoped to one repo, so it has no `repository`
        # field to return. Attach it here — consumers key PRs by repo name.
        for pr in prs or []:
            pr["repository"] = {"name": repo_name}
        all_prs.extend(prs or [])

    # 4. Recent commits (last 7 days) — only from tracked repos
    # Use timezone-aware UTC and a Zulu timestamp GitHub accepts. Avoid
    # datetime.utcnow().isoformat()+"Z" (naive + micros) which made string
    # comparisons against GitHub's pushedAt brittle.
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_commits: List[Dict] = []
    recent = [r for r in active if r.get("pushedAt") and r["pushedAt"] >= seven_days_ago]
    commit_lists = _gh_map(lambda repo: _gh_call([
        "api", f"/repos/{owner}/{repo.get('name', '')}/commits",
        "-q", ".",
        "--method", "GET",
        "-f", f"since={seven_days_ago}",
        "-f", f"sha={(repo.get('defaultBranchRef') or {}).get('name', 'main')}",
        "-f", "per_page=20",
    ], timeout=15), recent)
    for repo, (commits, failure) in zip(recent, commit_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            fetch_errors.append(f"recent commits for {owner}/{repo_name}")
        if commits and isinstance(commits, list):
            for c in commits[:20]:
                recent_commits.append({
                    "repo": repo_name,
                    "sha": c.get("sha", "")[:7],
                    "message": (c.get("commit", {}).get("message") or "").split("\n")[0],
                    "author": (c.get("commit", {}).get("author") or {}).get("name", ""),
                    "date": (c.get("commit", {}).get("author") or {}).get("date", ""),
                    "url": c.get("html_url", ""),
                })

    # 5. Workflow runs (CI status) — recent runs across tracked repos
    workflow_runs: List[Dict] = []
    run_lists = _gh_map(lambda repo: _gh_call([
        "api", f"/repos/{owner}/{repo.get('name', '')}/actions/runs",
        "-q", ".",
        "--method", "GET",
        "-f", "per_page=5",
    ], timeout=10), active)
    for repo, (runs, failure) in zip(active, run_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            # Without this a rate-limited runs endpoint empties workflow_runs,
            # silently blanking the Failing CI panel and undercounting failing_ci.
            fetch_errors.append(f"CI runs for {owner}/{repo_name}")
        if runs and isinstance(runs, dict):
            for run in (runs.get("workflow_runs") or [])[:5]:
                workflow_runs.append({
                    "repo": repo_name,
                    "name": run.get("name", ""),
                    "status": run.get("status", ""),
                    "conclusion": run.get("conclusion"),
                    "branch": run.get("head_branch", ""),
                    "created_at": run.get("created_at", ""),
                    "url": run.get("html_url", ""),
                })

    # 6. Branch info — only tracked repos
    branch_info: List[Dict] = []
    branch_lists = _gh_map(lambda repo: _gh_call([
        "api", f"/repos/{owner}/{repo.get('name', '')}/branches",
        "-q", ".",
        "--method", "GET",
        "-f", "per_page=100",
    ], timeout=10), active)
    for repo, (branches, failure) in zip(active, branch_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            fetch_errors.append(f"branches for {owner}/{repo_name}")
        if branches and isinstance(branches, list):
            for b in branches:
                branch_info.append({
                    "repo": repo_name,
                    "name": b.get("name", ""),
                    "protected": b.get("protected", False),
                })

    return {
        "user": {
            "login": user.get("login"),
            "name": user.get("name"),
            "avatar_url": user.get("avatar_url"),
            "public_repos": user.get("public_repos"),
            "private_repos": user.get("total_private_repos"),
            "followers": user.get("followers"),
            "following": user.get("following"),
        },
        "repos": repos,
        "tracked_projects": tracked_names,
        "open_pull_requests": all_prs,
        "recent_commits": recent_commits,
        "workflow_runs": workflow_runs,
        "branches": branch_info,
        "fetch_errors": fetch_errors,
        "summary": {
            "total_repos": len(repos),
            "tracked_projects": len(tracked_names),
            "repos_found_on_github": len(repos),
            "repos_not_on_github": repos_missing,
            "archived_repos": sum(1 for r in repos if r.get("isArchived")),
            "open_prs": len(all_prs),
            "draft_prs": sum(1 for p in all_prs if p.get("isDraft")),
            "recent_commit_count": len(recent_commits),
            "repos_with_ci": len(set(r["repo"] for r in workflow_runs)),
            "fetch_errors": len(fetch_errors),
            "failing_ci": sum(
                1 for r in workflow_runs
                if r.get("conclusion") == "failure"
                and (r.get("branch") in ("main", "master")
                     or any(pr.get("headRefName") == r.get("branch")
                            and pr.get("repository", {}).get("name") == r.get("repo")
                            for pr in all_prs))
            ),
        },
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "cached": False,
    }


@app.get("/api/github")
async def api_github():
    """Return the last snapshot immediately while one worker refreshes GitHub.

    A cold response has no repository data and ``refreshing: true``. Clients
    poll using retry_after_seconds until the snapshot arrives. Failed refreshes
    preserve old data and expose refresh_error rather than emptying the page.
    """
    return _github_cache.read(_fetch_github_data)
