"""Helpers every dashboard route area shares: logging, write serialization, admin and memory-store guards, display IDs, page shells, navigation."""

import asyncio
import functools
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from db.manager import DatabaseManager
from fastapi import HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from scripts.config import (
    MEMORY_NOT_CONFIGURED,
    MemoryIntegration,
    local_config_path,
    memory_integration,
)
from scripts.logger import get_logger


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


logger = get_logger(__name__)


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


# Setup templates and static files
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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


MARKERS_PATH = Path(__file__).parent.parent / "data" / "agentic_markers.json"
