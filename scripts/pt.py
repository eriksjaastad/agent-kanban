#!/usr/bin/env python3
"""
Project Tracker CLI - Track all your projects in one place.

How to Use:
-----------
1. Run with Launcher (easiest):
   pt [command]

2. Run with Python directly:
   python scripts/pt.py [command]

Common Commands:
- pt scan      # Scan for new projects and rebuild graph
- pt launch    # Start the web dashboard
- pt list      # List all projects in terminal
- pt --help    # Show help
"""

import copy
import os
import shlex
import shutil
import sqlite3
import sys
import webbrowser
import time
import json
from datetime import datetime, timezone as _timezone
from pathlib import Path
from typing import Any, Callable, List, NoReturn, Optional
import subprocess

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.progress import Progress

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from scripts.config import (MEMORY_NOT_CONFIGURED, PROJECTS_BASE_DIR, chat_addresses, digest_integration, local_config,
                            local_config_path, memory_integration, morning_integration, tailor_rules, fit_keywords,
                            graph_settings)
from db.schema import (
    init_db, get_db_path, SafetyError, FreshDatabaseError,
)
from db.manager import DatabaseManager
from db.checkouts import MACHINE_LABELS, CheckoutsMigrationPendingError, ProjectCheckedOut
from db.backend_calendar_manager import validate_event_date, validate_event_time
from discovery.project_scanner import (
    discover_projects,
    extract_project_metadata,
    scan_health_parallel,
)
from discovery.external_resources_parser import parse_external_resources
from discovery.graph_builder import GraphBuilder
from discovery.backup_reader import get_backup_status
# update_directory_index removed — 00_Index generation killed (#5530)
from utils.validation import BlockedTaskProjectError
from origin import resolve_created_by as _resolve_created_by
from skill_invocations_reader import (
    iter_invocations as _iter_skill_invocations,
    SkillInvocationsTableMissing as _SkillInvocationsTableMissing,
)
from skills_registry import installed_skills as _installed_skills

# Local operations expose validation and safety errors directly, without RPC wrapping.
_LOCAL_DB_ERRORS = (
    sqlite3.Error, OSError, RuntimeError, ValueError,
    SafetyError, FreshDatabaseError,
)

console = Console()
PT_VERSION = "0.0.0"
PT_JSON_SCHEMA_VERSION = "pt.v1"
PT_MEMORY_SCHEMA_VERSION = "pt.memory.v1"
PT_CONFIG_SCHEMA_VERSION = "pt.config.v1"
PT_DOCTOR_SCHEMA_VERSION = "pt.doctor.v1"
PT_HYGIENE_SCHEMA_VERSION = "pt.hygiene.v1"

EXIT_VALIDATION = 2
EXIT_BACKEND_UNAVAILABLE = 3
EXIT_QUERY_FAILURE = 4
EXIT_AUTH = 5
EXIT_HYGIENE_FINDING = 6


def _resolve_project_directory(project_name: str) -> Optional[Path]:
    """Resolve a project directory under the projects root."""
    direct_path = Path(PROJECTS_BASE_DIR) / project_name
    return direct_path if direct_path.is_dir() else None


def _resolve_dashboard_python(project_root: Path) -> str:
    """Resolve the Python interpreter used to launch the dashboard."""
    candidates = [
        project_root / ".venv" / "bin" / "python",
        project_root / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    console.print(
        f"[red]ERROR: No .venv or venv found under {project_root}. "
        "Create a virtual environment before launching the dashboard.[/red]"
    )
    sys.exit(1)



_PT_BANNER = """\
[bold cyan]  /^\\\\[/bold cyan]
[bold cyan] (   )[/bold cyan]  [bold white]pt[/bold white] [dim]· project tracker[/dim]
[bold cyan]  ) ([/bold cyan]
[bold cyan] (___)  [/bold cyan][dim]✦ plan · ship · repeat ✦[/dim]
"""

def _print_banner() -> None:
    """Print the pt banner — suppressed in non-interactive / CI environments."""
    import os, sys
    if not sys.stdout.isatty():
        return
    if os.environ.get("CI") or os.environ.get("PT_NO_BANNER"):
        return
    console.print(_PT_BANNER)


@click.group(invoke_without_command=True)
@click.version_option(PT_VERSION, "--version", prog_name="pt")
@click.pass_context
def cli(ctx):
    """Project Tracker - Manage and track all your projects

    Environment Variables:
    ----------------------
    PT_DB_PATH: Path to the database file (default: data/tracker.db)
    PROJECTS_ROOT: Path to your projects folder
    PT_EXTERNAL_BACKUP_DIR: Path for safety backups (use this in sandboxed environments)
    PT_ALLOW_FRESH_DB: Set to 1 to allow starting with an empty database
    SAFE_MODE: Set to 0 to enable permanent deletions (the owner only)
    PT_NO_BANNER: Set to 1 to suppress the startup banner
    """
    _print_banner()
    # Phase 2.1a step 5 — surface any unapplied migrations so the
    # operator knows to run `pt db migrate`. Skipped when the user is
    # already running `db migrate` (the fix) or when explicitly silenced.
    # Automation callers (cron/SSH) set PT_SUPPRESS_MIGRATION_WARNING=1
    # to silence; the warning otherwise goes to stderr and never pollutes
    # stdout JSON output.
    if ctx.invoked_subcommand not in ("db",) and not os.environ.get(
        "PT_SUPPRESS_MIGRATION_WARNING"
    ):
        _warn_unapplied_migrations()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _warn_unapplied_migrations() -> None:
    """Print a stderr warning if the tracker DB has pending migrations.

    Never raises — a warning failure must not take `pt` down.
    """
    try:
        pending = DatabaseManager().migrations_pending()
        if not pending:
            return
        names = ", ".join(f"{m['version']:03d}_{m['name']}" for m in pending)
        click.echo(
            f"⚠ pending migration(s): {names}. Run `pt db migrate` to apply.",
            err=True,
        )
    # governance: allow-silent SF002: an advisory pre-command warning; the failure is printed to stderr and must not block the requested pt command
    except Exception as err:
        # A broken migration file or broken DB config shouldn't brick
        # the CLI, but it shouldn't be silent either — the operator
        # needs to see that something is wrong, not just a missing
        # warning. Log and continue.
        click.echo(
            f"⚠ could not check for pending migrations: {err!r}",
            err=True,
        )
        return


def _load_external_services() -> dict:
    """parse_external_resources(), or a clean abort before any database write.

    The services sync deletes rows missing from this result, so an unreadable
    or missing registry must stop the scan rather than look like "no services"
    (#6900).
    """
    import yaml as _yaml

    try:
        return parse_external_resources()
    except (OSError, ValueError, _yaml.YAMLError) as exc:
        console.print(
            f"[red]Cannot load the external resources registry: {escape(str(exc))}[/red]\n"
            "[red]Aborting before any database write so existing service rows are kept.[/red]"
        )
        raise SystemExit(1)


GRAPH_PATH = Path(__file__).parent.parent / "data" / "graph.json"
GRAPH_ANALYSIS_PATH = Path(__file__).parent.parent / "data" / "graph_analysis.md"


def rebuild_project_graph():
    """Rebuild data/graph.json and its analysis.

    A failure raises: the dashboard's tracked rebuild records it as an error,
    and the CLI commands exit 1 (_rebuild_graph_or_exit). The old graph is
    left as it was.
    """
    console.print("[bold blue]Rebuilding project graph & analysis...[/bold blue]")
    builder = GraphBuilder(Path(PROJECTS_BASE_DIR))
    builder.scan()
    builder.save(GRAPH_PATH)
    GRAPH_ANALYSIS_PATH.write_text(builder.generate_analysis())
    console.print(f"  ✓ Project graph & analysis updated")


def _rebuild_graph_or_exit():
    """rebuild_project_graph() for a CLI command: a failure is printed and exits 1."""
    try:
        rebuild_project_graph()
    except Exception as e:
        console.print(f"  [red]✗ Error rebuilding project graph: {escape(str(e))}[/red]")
        raise SystemExit(1) from e


def _scan_impl(no_graph=False, dry_run=False, force=False):
    console.print(f"[bold blue]Scanning projects in {PROJECTS_BASE_DIR}...[/bold blue]")
    base_path = Path(PROJECTS_BASE_DIR)
    if not base_path.exists() or not base_path.is_dir():
        console.print("[red]PROJECTS_BASE_DIR is invalid or missing. Aborting scan.[/red]")
        return
    # Report an unavailable database before scanning.
    try:
        db = DatabaseManager()
        db.health_snapshot()
        db_reachable = True
    except _LOCAL_DB_ERRORS as exc:  # governance: allow-silent SF002: top-level `pt scan`; the red "Database unavailable" line reports the failure and the scan aborts before any write, like its other abort paths
        db_reachable = False
        if not dry_run:
            console.print(f"[red]Database unavailable: {exc}[/red]")
            return
        console.print(
            f"[yellow]Database unavailable; dry-run will skip DB comparison.[/yellow]\n"
            f"[dim]{exc}[/dim]"
        )
    with Progress() as progress:
        task = progress.add_task("[cyan]Discovering projects...", total=None)
        projects = discover_projects(PROJECTS_BASE_DIR)
        progress.update(task, completed=True)
    console.print(f"\n[green]Found {len(projects)} projects[/green]\n")
    existing_projects = db.get_all_projects() if db_reachable else []
    existing_count = len(existing_projects)
    if not force:
        if len(projects) == 0:
            subdir_count = len([p for p in base_path.iterdir() if p.is_dir() and not p.name.startswith(".")])
            console.print("[red]Scan found 0 projects. Aborting to prevent accidental data loss.[/red]")
            console.print(f"[yellow]Detected {subdir_count} subdirectories under PROJECTS_BASE_DIR.[/yellow]")
            console.print("[yellow]Re-run with --force if this is expected.[/yellow]")
            return
        if existing_count > 0 and len(projects) < max(1, int(existing_count * 0.5)):
            console.print("[red]Scan found far fewer projects than expected. Aborting to prevent data loss.[/red]")
            console.print(f"[yellow]Previous count: {existing_count}, New count: {len(projects)}[/yellow]")
            console.print("[yellow]Re-run with --force if this is expected.[/yellow]")
            return
    if dry_run:
        console.print("[dim]Dry-run: skipping health checks and database writes.[/dim]")
        return
    with Progress() as progress:
        task = progress.add_task("[cyan]Auditing project health...", total=len(projects))
        health_results = scan_health_parallel(projects)
        progress.update(task, advance=len(projects))
    existing_ids = {p["id"] for p in existing_projects}
    discovered_ids = {p["id"] for p in projects}
    stale_ids = existing_ids - discovered_ids
    if stale_ids:
        console.print(f"  [dim]ℹ {len(stale_ids)} projects not found in scan (preserved in DB)[/dim]")
    console.print(f"\n[bold blue]Loading services from EXTERNAL_RESOURCES.md...[/bold blue]")
    services_by_project = _load_external_services()
    if not db_reachable:
        # Dry run with no service: everything above this point was discovery,
        # which is filesystem work and still worth showing. Writing is not
        # possible and must not be silently skipped as if it had happened.
        console.print(
            "\n[yellow]Dry run complete. No projects were written — the database "
            "service is unavailable.[/yellow]"
        )
        return

    for project in projects:
        result = db.sync_project_bundle(
            project=project,
            agents=project.get("ai_agents", []),
            cron_jobs=project.get("cron_jobs", []),
            services=services_by_project.get(project["id"], []),
            health=health_results.get(project["id"]),
        )
        if result["ok"]:
            console.print(f"  ✓ {project['name']}")
        else:
            console.print(
                f"  [red]✗ Failed to update {project['name']}: {result['error']}[/red]"
            )
            continue
    services_skipped = 0
    known_project_ids = {p["id"] for p in db.get_all_projects()}
    for project_id in services_by_project.keys():
        if project_id not in known_project_ids:
            services_skipped += 1
            console.print(f"  [yellow]! Skipping services for unknown project: {project_id}[/yellow]")
    if services_skipped > 0:
        console.print(f"  [yellow]! Skipped services for {services_skipped} unknown projects[/yellow]")
    if not no_graph:
        console.print("")
        _rebuild_graph_or_exit()
    console.print(f"\n[bold green]✅ Scan complete! {len(projects)} projects updated[/bold green]")


def _has_complete_prompt(prompt):
    if not prompt: return False
    prompt_lower = prompt.lower()
    return "## overview" in prompt_lower and "## execution" in prompt_lower and "## done criteria" in prompt_lower



def _coerce_display_map(display_map) -> dict:
    """JSON-RPC may stringify snowflake dict keys; coerce to int."""
    if not display_map:
        return {}
    return {int(k): v for k, v in display_map.items()}


def _display_id_map(db, task_ids) -> dict:
    """{snowflake_pk: display_id} for the given ids (empty if no db/ids)."""
    ids = [i for i in task_ids if i is not None]
    if not db or not ids:
        return {}
    return _coerce_display_map(db.get_task_display_id_map(ids))


def _format_task_ref(task_id, display_map=None, db=None) -> str:
    """Human card number: prefer 4-digit display_id over snowflake pk."""
    if display_map is None and db is not None:
        display_map = _display_id_map(db, [task_id])
    display_map = display_map or {}
    label = display_map.get(int(task_id), task_id) if task_id is not None else task_id
    return f"#{label}"


def _format_task_refs(task_ids, db=None) -> str:
    """Comma-separated human card refs for a list of snowflake pks."""
    ids = list(task_ids or [])
    display_map = _display_id_map(db, ids)
    return ", ".join(_format_task_ref(i, display_map=display_map) for i in ids)


def _blocked_state(db, task_id) -> tuple:
    """(is_blocked, blocking_ids, reason) for one card, failing closed (#6900).

    An unreadable blocked_by, or a failed blocker lookup, counts as BLOCKED
    and carries a reason instead of ids. `pt tasks start` refuses with that
    reason; listings and the tree still render the card, marked blocked.
    """
    try:
        is_blocked, blocking_ids = db.is_blocked(task_id)
        if is_blocked and not blocking_ids:
            return True, [], db.blocked_by_error(task_id) or "blocked_by unreadable"
    except Exception as exc:  # noqa: BLE001 - fail closed; never render as unblocked
        return True, [], f"blocked state unreadable: {exc}"
    return is_blocked, list(blocking_ids), None


def _blocked_label(blocking_ids, reason, display_map) -> str:
    """Text after `[B:` in listings: blocker card numbers, or `unreadable`."""
    if reason:
        return "unreadable"
    return ",".join(str(display_map.get(int(i), i)) for i in blocking_ids)

def _checkout_label(checkout: dict) -> str:
    """`CHECKED OUT — Mac Mini (Qwen on Mini, since 2026-10-16)`, the one wording every surface prints."""
    return (
        f"CHECKED OUT — {MACHINE_LABELS[checkout['machine']]} "
        f"({checkout['holder']}, since {checkout['checked_out_at'][:10]})"
    )


def _checkout_marker(checkouts: dict, project_id: str) -> str:
    """`[OUT: Mac Mini] ` for a card whose project is checked out, else empty."""
    checkout = checkouts.get(project_id)
    return f"[OUT: {MACHINE_LABELS[checkout['machine']]}] " if checkout else ""


def _print_checkin_reminders(db, reminders: list) -> None:
    """One line per checkout due for check-in (#8626), so its holder sees it."""
    for due in reminders:
        # --checkout-id, so a stale reminder can't close a newer checkout of the same project.
        # Options, then `--`, then the id, so an id like -x or --help is never read as an option;
        # shlex.join, so one like r&d reaches the CLI as one argument.
        command = shlex.join(["pt", "project", "checkin", "--checkout-id", str(due["id"]), "--by", "<you>",
                              "--", due["project_id"]])
        console.print(
            f"[bold yellow]Check in {escape(due['project_id'])}: out to {MACHINE_LABELS[due['machine']]} "
            f"({escape(due['holder'])}) for {_format_task_ref(due['task_id'], db=db)}, and {due['reason']}. "
            f"Run: {escape(command)}[/bold yellow]",
            soft_wrap=True,  # one line, so the command can be copied and grepped
            emoji=False,  # or a project id like :smile: prints as an emoji
        )


def _display_tasks(task_list, project=None, json_output=False, db=None, checkouts=None):
    import json as json_lib
    checkouts = checkouts or {}
    if json_output:
        for task in task_list:
            task["checkout"] = checkouts.get(task["project_id"])
        backend = "local"
        print(json_lib.dumps({"tasks": task_list, "total": len(task_list), "backend": backend}, indent=2))
        return
    if not task_list:
        filter_msg = f" for project '{project}'" if project else ""
        print(f"No tasks found{filter_msg}.")
        return
    backend_tag = "local"
    title = f"Tasks - {project}" if project else "Tasks - all projects"
    print(f"{title} [{backend_tag}]\n")
    # Include blocker ids in the display map so [B:…] stays four-digit friendly.
    map_ids = [t["id"] for t in task_list]
    blocked_states = {}
    if db:
        for task in task_list:
            if task.get("blocked_by"):
                state = _blocked_state(db, task["id"])
                blocked_states[task["id"]] = state
                if state[0]:
                    map_ids.extend(state[1])
    display_map = _display_id_map(db, map_ids)
    for task in task_list:
        priority = task.get("priority") or "-"
        status = task["status"]
        task_id = task["id"]
        task_text = task["text"]
        label = display_map.get(int(task_id), task_id)
        if _has_complete_prompt(task.get("prompt")): prompt_marker = ""
        elif task.get("prompt"): prompt_marker = "[~P] "
        else: prompt_marker = "[!P] "
        blocked_marker = ""
        if db and task.get("blocked_by"):
            is_blocked, blocking_ids, reason = blocked_states[task_id]
            if is_blocked:
                b_labels = _blocked_label(blocking_ids, reason, display_map)
                blocked_marker = f"[B:{b_labels}] "
        if project:
            print(f"#{label} {prompt_marker}{blocked_marker}| {status} | {priority} | {task_text}")
        else:
            out_marker = _checkout_marker(checkouts, task["project_id"])
            print(f"#{label} {prompt_marker}{blocked_marker}{out_marker}| {task['project_id']} | {status} | {priority} | {task_text}")
    status_counts = {}
    for task in task_list:
        status_counts[task["status"]] = status_counts.get(task["status"], 0) + 1
    summary_parts = [f"{s}: {status_counts[s]}" for s in ["Backlog", "To Do", "In Progress", "Review", "Done", "Cancelled"] if s in status_counts]
    print(f"\nTotal: {len(task_list)} tasks ({', '.join(summary_parts)})")


def _resolve_project_id(db, project):
    """Resolve a project id or name (case-insensitive) to its id, or None.

    An id match wins over a name match, so a project named after another
    project's id can't capture it. add_project keeps names unique, but a
    concurrent add can still leave two; a shared name is refused rather than
    picked by row order.
    """
    if not project: return None
    wanted = project.lower()
    projects = db.get_all_projects()
    for field in ("id", "name"):
        matches = [p["id"] for p in projects if p[field].lower() == wanted]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise click.UsageError(
                f"Project {field} '{project}' matches {len(matches)} projects "
                f"({', '.join(matches)}); use the exact id"
            )
    return None


def _resolve_task_id(db, token: int) -> int:
    """Resolve a display_id or Snowflake task_id to the canonical task PK.

    Returns the resolved task_id, or token unchanged if resolution fails
    (callers that pass a bad ID will get a "not found" from get_task anyway).
    """
    resolved = db.resolve_task_id(token)
    return resolved if resolved is not None else token


def _parse_blocked_by(db, blocked_by: str) -> str:
    """Turn a `--blocked-by` token list into a JSON array of canonical task PKs.

    Two things happen here, and both are load-bearing (#6747):

    1. Every token goes through ``db.resolve_task_id`` so display IDs work the
       way they do in every other command. `--blocked-by 6742` used to store
       the display ID verbatim, which matches no row.
    2. A token that resolves to nothing is refused outright. Storing it makes
       ``get_blocking_tasks`` drop it silently, ``is_blocked`` return
       ``(False, [])``, and the board report the card as unblocked while it is
       genuinely blocked — the exact opposite of the truth.

    Raises ``click.ClickException`` on anything unparseable or unresolvable.
    """
    import json as _json

    tokens = [t.strip() for t in blocked_by.split(",") if t.strip()]
    if not tokens:
        raise click.ClickException(
            "--blocked-by must be comma-separated task IDs (e.g., '4645,4646')"
        )

    resolved: List[int] = []
    unknown: List[str] = []
    for token in tokens:
        try:
            value = int(token)
        except ValueError:
            raise click.ClickException(
                "--blocked-by must be comma-separated task IDs "
                f"(e.g., '4645,4646'); got {token!r}"
            )
        task_pk = db.resolve_task_id(value)
        if task_pk is None:
            unknown.append(token)
        else:
            resolved.append(task_pk)

    if unknown:
        plural = "IDs" if len(unknown) > 1 else "ID"
        raise click.ClickException(
            f"--blocked-by names {plural} that match no task: "
            f"{', '.join('#' + u for u in unknown)}. Storing an unresolvable ID "
            f"would make this card report itself unblocked while it is blocked."
        )

    return _json.dumps(resolved)


def _blocked_by_report(db, task) -> Optional[str]:
    """One "Blocked by:" line for a task, or None when it has no blockers.

    Three outcomes are kept distinct so this can never again print the exact
    opposite of the truth (#6747). Before the write-time resolution fix, an
    unresolvable stored ID was silently dropped by ``get_blocking_tasks``,
    ``is_blocked`` answered ``(False, [])``, and this line read
    "(all resolved)" for a card that was genuinely blocked.
    """
    import json as _json

    raw = task.get("blocked_by")
    if not raw:
        return None
    try:
        stored = _json.loads(raw)
    except (TypeError, ValueError):
        return f"Blocked by: (malformed blocked_by value: {raw!r})"
    if not isinstance(stored, list):
        return f"Blocked by: (malformed blocked_by value: {raw!r})"
    if not stored:
        return None

    incomplete: List = []
    unknown: List = []
    for tid in stored:
        blocker = db.get_task(tid) if isinstance(tid, int) else None
        if blocker is None:
            unknown.append(tid)
        elif blocker["status"] != "Done":
            incomplete.append(tid)

    parts = []
    if incomplete:
        parts.append(f"{', '.join(f'#{t}' for t in incomplete)} (incomplete)")
    if unknown:
        parts.append(
            f"{', '.join(f'#{t}' for t in unknown)} (UNRESOLVED - no such task; "
            f"this card's blocked state cannot be trusted, re-set it with "
            f"`pt tasks update <id> --blocked-by ...`)"
        )
    if not parts:
        return "Blocked by: (all resolved)"
    return "Blocked by: " + "; ".join(parts)


def _detect_project_from_cwd(db):
    import os
    cwd = os.environ.get("PT_CALLER_CWD", os.getcwd())
    dir_name = os.path.basename(cwd)
    try:
        return _resolve_project_id(db, dir_name)
    except click.UsageError as e:
        # The caller typed no project; say where the name came from and how to pick.
        raise click.UsageError(
            f"Can't tell the project from directory '{dir_name}': {e.message}. Pass -p <id>."
        ) from e


# =============================================================================
# Top-level commands
# =============================================================================

def _hidden_alias(command: click.Command, name: str) -> click.Command:
    """The same command (same callback and options) under a second, hidden name."""
    alias = copy.copy(command)
    alias.name = name
    alias.hidden = True
    return alias


@cli.command()
def init():
    """Initialize the project tracker database."""
    console.print("[bold green]Initializing project tracker...[/bold green]")
    db_path = init_db()
    console.print(f"✅ Database created at: {db_path}")


@cli.command(name="scan")
@click.option("--no-graph", is_flag=True, help="Skip rebuilding the project graph")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing")
@click.option("--force", is_flag=True, help="Proceed even if scan results look unsafe")
def scan_cli(no_graph, dry_run, force):
    """Scan the projects directory and update the database.

    Rescans every repo under PROJECTS_ROOT and rebuilds the project graph
    (skip that with --no-graph). For a single project, `pt sync-project NAME`
    is faster.
    """
    _scan_impl(no_graph=no_graph, dry_run=dry_run, force=force)


# `pt refresh` is the old spelling of `pt scan`: the same command object under a
# second, hidden name, so the two cannot drift apart.
cli.add_command(_hidden_alias(scan_cli, "refresh"))


@cli.command(name="list")
def list_projects():
    """List all projects."""
    db = DatabaseManager()
    projects = db.get_all_projects()
    if not projects:
        print("No projects found. Run 'pt scan' first.")
        return
    checkouts = db.get_open_checkouts()
    print("Projects\n")
    for project in projects:
        idx = "✓" if project.get("has_index") and project.get("index_is_valid") else "!"
        phase = project.get("phase") or "-"
        pct = project.get("completion_pct", 0)
        checkout = checkouts.get(project["id"])
        out = f" | {_checkout_label(checkout)}" if checkout else ""
        print(f"{project['name']} | {project['status']} | {phase} | {pct}% | {idx}{out}")
    print(f"\nTotal: {len(projects)} projects")


@cli.command()
@click.argument("name")
def status(name):
    """Show detailed status for a project."""
    db = DatabaseManager()
    project_id = _resolve_project_id(db, name)
    project = db.get_project(project_id) if project_id else None
    if not project:
        console.print(f"[red]Project '{name}' not found[/red]")
        return
    console.print(f"\n[bold cyan]{project['name']}[/bold cyan]")
    checkout = db.get_open_checkouts().get(project["id"])
    if checkout:
        console.print(f"[bold red]{escape(_checkout_label(checkout))}[/bold red]")
    console.print(f"Path: {project['path']}")
    console.print(f"Status: [green]{project['status']}[/green]")
    if project.get("phase"): console.print(f"Phase: {project['phase']}")
    console.print(f"Progress: {project.get('completion_pct', 0)}%")
    console.print(f"Last Modified: {project.get('last_modified', 'unknown')}")
    if project.get("description"): console.print(f"\n{project['description']}")
    agents = db.get_ai_agents(project["id"])
    if agents:
        console.print("\n[bold]AI Agents:[/bold]")
        for agent in agents:
            role = f" - {agent['role']}" if agent.get('role') else ""
            console.print(f"  • {agent['agent_name']}{role}")
    jobs = db.get_cron_jobs(project["id"])
    if jobs:
        console.print("\n[bold]Cron Jobs:[/bold]")
        for job in jobs:
            console.print(f"  • {job['schedule']}: {job['command']}")
    services = db.get_services(project["id"])
    if services:
        console.print("\n[bold]Services:[/bold]")
        for service in services:
            cost = f" (${service['cost_monthly']}/mo)" if service.get('cost_monthly') else ""
            console.print(f"  • {service['service_name']}{cost}")
    console.print()


@cli.command(name="sync-project")
@click.argument("project_name")
@click.option("--no-graph", is_flag=True, help="Skip rebuilding the project graph")
def sync_project(project_name, no_graph):
    """Sync one project to the database (fast alternative to full scan).

    PROJECT_NAME is the directory name under the projects root.
    """
    base_path = Path(PROJECTS_BASE_DIR)
    project_dir = _resolve_project_directory(project_name)

    if not project_dir or not project_dir.exists() or not project_dir.is_dir():
        console.print(f"[red]Directory not found for project '{project_name}' under {base_path}[/red]")
        raise SystemExit(1)

    db_path = get_db_path()
    if not db_path.exists():
        console.print("[red]Database not initialized. Run 'pt init' first.[/red]")
        raise SystemExit(1)

    console.print(f"[bold blue]Syncing project: {project_name}...[/bold blue]")

    # 1. Extract metadata (same as full scan)
    project = extract_project_metadata(project_dir)
    if not project:
        console.print(f"[red]Could not extract metadata from {project_dir}[/red]")
        raise SystemExit(1)

    # 2. Health check
    health_results = scan_health_parallel([project])

    # 3. Load external services for this project
    services_by_project = _load_external_services()

    # 4. Upsert to database
    db = DatabaseManager()
    result = db.sync_project_bundle(
        project=project,
        agents=project.get("ai_agents", []),
        cron_jobs=project.get("cron_jobs", []),
        services=services_by_project.get(project["id"], []),
        health=health_results.get(project["id"]),
    )
    if result["ok"]:
        console.print(f"  [green]✓ {project['name']}[/green]")
    else:
        console.print(f"  [red]✗ Failed to sync {project['name']}: {result['error']}[/red]")
        raise SystemExit(1)

    # 5. Optionally rebuild project graph
    if not no_graph:
        _rebuild_graph_or_exit()

    console.print(f"\n[bold green]✅ Synced: {project['name']}[/bold green]")


@cli.command()
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON (schema: pt.hygiene.v1)")
@click.option("--project", "project_name", default=None, help="Inspect only one repo by name")
@click.option("--quiet", is_flag=True, help="Only show repos with at least one finding")
def hygiene(json_output: bool, project_name: Optional[str], quiet: bool) -> None:
    """Check portfolio-wide git hygiene state. Read-only.

    \b
    pt hygiene --json                        portfolio-wide scan, JSON
    pt hygiene --json --project my-project   one repo
    pt hygiene --quiet                       human output, findings only

    Detects: dirty working tree (PROGRESS.md excluded), local-only branches,
    branches ahead of remote, stashes, open bot PRs older than 24h, and a
    stale PROGRESS.md (over 7 days old while the repo has uncommitted work).

    Exit 0 = all repos clean; exit 6 = at least one finding (for cron
    branching).

    --json emits schema pt.hygiene.v1. Each entry under "results" has one of
    two disjoint shapes:

    \b
    - scanned:  {project, path, clean, findings: {dirty_tree,
                local_only_branches, branches_ahead_of_remote, stashes,
                open_pr_drift, stale_progress_md}}  (all six keys present;
                each finding has "present")
    - failed:   {project, path, clean: false, error: {class, message}}
                ("findings" is omitted, so a broken repo never reads as
                checked-and-clean)

    Consumers must check for "error" before reading "findings".
    """
    from datetime import timezone

    # Read at runtime so test harnesses can override via env var
    # governance: allow-silent SF003: the override is optional; empty falls back to PROJECTS_BASE_DIR on the next line
    _projects_root_env = os.environ.get("PROJECTS_ROOT", "")
    projects_root = Path(_projects_root_env).resolve() if _projects_root_env.strip() else Path(PROJECTS_BASE_DIR)

    # Collect git repos to inspect
    if project_name:
        candidate = projects_root / project_name
        if not candidate.is_dir() or not (candidate / ".git").exists():
            err = PtJsonError(
                "validation",
                f"project '{project_name}' not found or not a git repo under {projects_root}",
                EXIT_VALIDATION,
            )
            if json_output:
                _emit_json_error(err, "hygiene")
            else:
                raise click.ClickException(err.message)
        repo_dirs = [candidate]
    else:
        repo_dirs = sorted(
            d for d in projects_root.iterdir() if d.is_dir() and (d / ".git").exists()
        )

    results = []
    for repo_dir in repo_dirs:
        try:
            repo_result = _hygiene_scan_repo(repo_dir, json_output)
        except Exception as exc:  # noqa: BLE001 — isolate per-repo failures
            # Schema contract: error entries OMIT the "findings" key entirely.
            # Consumers MUST check for "error" before accessing "findings".
            # An empty findings dict would falsely read as "we checked and
            # found nothing"; absence reads correctly as "we couldn't check."
            repo_result = {
                "project": repo_dir.name,
                "path": str(repo_dir),
                "clean": False,
                "error": {
                    "class": exc.__class__.__name__,
                    "message": str(exc),
                },
            }
        results.append(repo_result)

    clean_count = sum(1 for r in results if r["clean"])
    findings_count = len(results) - clean_count
    has_findings = findings_count > 0

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if json_output:
        payload = {
            "schema_version": PT_HYGIENE_SCHEMA_VERSION,
            "ok": not has_findings,
            "command": "hygiene",
            "scanned_at": now_utc,
            "projects_root": str(projects_root),
            "summary": {
                "total_repos": len(results),
                "clean_repos": clean_count,
                "repos_with_findings": findings_count,
            },
            "results": results,
        }
        _emit_json(payload)
    else:
        for r in results:
            if quiet and r["clean"]:
                continue
            status = "clean" if r["clean"] else "FINDINGS"
            console.print(f"[bold]{r['project']}[/bold] ({r['path']}) — {status}")
            if not r["clean"]:
                if "error" in r:
                    err = r["error"]
                    console.print(f"  [red]scan error ({err['class']}):[/red] {err['message']}")
                    continue
                f = r["findings"]
                if f["dirty_tree"]["present"]:
                    files = ", ".join(f["dirty_tree"]["files"][:5])
                    console.print(f"  [yellow]dirty tree:[/yellow] {files}")
                if f["local_only_branches"]["present"]:
                    branches = ", ".join(f["local_only_branches"]["branches"])
                    console.print(f"  [yellow]local-only branches:[/yellow] {branches}")
                if f["branches_ahead_of_remote"]["present"]:
                    ahead = ", ".join(
                        f"{b['name']} (+{b['ahead']})"
                        for b in f["branches_ahead_of_remote"]["branches"]
                    )
                    console.print(f"  [yellow]branches ahead:[/yellow] {ahead}")
                if f["stashes"]["present"]:
                    console.print(f"  [yellow]stashes:[/yellow] {f['stashes']['count']}")
                if f["open_pr_drift"].get("present"):
                    for pr in f["open_pr_drift"]["prs"]:
                        console.print(
                            f"  [yellow]open bot PR #{pr['number']}[/yellow]: {pr['title']} "
                            f"({pr['age_hours']:.1f}h old)"
                        )
                if f["stale_progress_md"]["present"]:
                    console.print(
                        f"  [yellow]stale PROGRESS.md:[/yellow] {f['stale_progress_md']['age_days']} days old"
                    )

    if has_findings:
        raise SystemExit(EXIT_HYGIENE_FINDING)


def _hygiene_scan_repo(repo_dir: Path, json_mode: bool) -> dict:
    """Run all hygiene detections for a single git repo directory."""
    findings = {
        "dirty_tree": _hygiene_dirty_tree(repo_dir),
        "local_only_branches": _hygiene_local_only_branches(repo_dir),
        "branches_ahead_of_remote": _hygiene_branches_ahead(repo_dir),
        "stashes": _hygiene_stashes(repo_dir),
        "open_pr_drift": _hygiene_open_pr_drift(repo_dir),
        "stale_progress_md": _hygiene_stale_progress_md(repo_dir),
    }
    clean = not any(
        (v.get("present", False) if isinstance(v, dict) else False)
        for v in findings.values()
    )
    return {
        "project": repo_dir.name,
        "path": str(repo_dir),
        "clean": clean,
        "findings": findings,
    }


def _run_git(repo_dir: Path, args: list[str], timeout: int = 10) -> tuple[str, int]:
    """Run a git command in repo_dir. Returns (stdout, returncode)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout, result.returncode


def _hygiene_non_progress_dirty_files(repo_dir: Path) -> list[str]:
    """Return dirty file paths from `git status --porcelain`, excluding PROGRESS.md.

    Shared by `_hygiene_dirty_tree` (for the finding) and
    `_hygiene_stale_progress_md` (to decide whether the repo has uncommitted
    work other than PROGRESS.md). A git failure raises, so `pt hygiene`
    records the repo as a scan error instead of reporting it clean.
    """
    stdout, rc = _run_git(repo_dir, ["status", "--porcelain"])
    if rc != 0:
        raise RuntimeError(f"git status --porcelain exited {rc} in {repo_dir}")

    dirty_files: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        # porcelain format: XY filename (or "XY src -> dst" for renames).
        # columns 0-1 are status codes, column 2 is space, rest is filename.
        path_part = line[3:].strip()
        # For renames: "old -> new", take the new path
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        # Exclude PROGRESS.md (documented always-dirty file)
        if path_part == "PROGRESS.md" or path_part.endswith("/PROGRESS.md"):
            continue
        dirty_files.append(path_part)

    return dirty_files


def _hygiene_dirty_tree(repo_dir: Path) -> dict:
    """Detect dirty working tree, excluding PROGRESS.md."""
    dirty_files = _hygiene_non_progress_dirty_files(repo_dir)
    return {"present": bool(dirty_files), "files": dirty_files}


def _hygiene_local_only_branches(repo_dir: Path) -> dict:
    """Detect local branches with no upstream tracking remote."""
    try:
        stdout, rc = _run_git(
            repo_dir, ["branch", "--format=%(refname:short) %(upstream)"]
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"present": False, "branches": []}

    local_only = []
    for line in stdout.splitlines():
        parts = line.strip().split(" ", 1)
        branch = parts[0]
        upstream = parts[1].strip() if len(parts) > 1 else ""
        # Skip main/master/trunk — they may not have upstreams in some setups
        if branch in ("main", "master", "trunk"):
            continue
        if not upstream:
            local_only.append(branch)

    return {"present": bool(local_only), "branches": local_only}


def _hygiene_branches_ahead(repo_dir: Path) -> dict:
    """Detect local branches with commits not yet on their upstream.

    Strategy: list branches with upstreams via for-each-ref, then for each
    one, count `git rev-list --count <upstream>..<branch>`. This works even
    when `origin/HEAD` is missing (common in fresh clones / test fixtures)
    and avoids relying on the for-each-ref %(ahead-behind:...) atom which
    requires a concrete ref argument.
    """
    try:
        stdout, rc = _run_git(
            repo_dir,
            ["for-each-ref", "--format=%(refname:short)|%(upstream:short)", "refs/heads"],
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"present": False, "branches": []}

    ahead_branches = []
    for line in stdout.splitlines():
        if "|" not in line:
            continue
        branch, upstream = line.split("|", 1)
        branch = branch.strip()
        upstream = upstream.strip()
        if not upstream:
            # No upstream -> handled by local_only_branches finding instead
            continue
        try:
            count_out, count_rc = _run_git(
                repo_dir, ["rev-list", "--count", f"{upstream}..{branch}"]
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if count_rc != 0:
            continue
        try:
            ahead = int(count_out.strip())
        except ValueError:
            continue
        if ahead > 0:
            ahead_branches.append({"name": branch, "ahead": ahead})

    return {"present": bool(ahead_branches), "branches": ahead_branches}


def _hygiene_stashes(repo_dir: Path) -> dict:
    """Detect stashes (count > 0)."""
    try:
        stdout, rc = _run_git(repo_dir, ["stash", "list"])
    except (subprocess.TimeoutExpired, OSError):
        return {"present": False, "count": 0}

    count = len([line for line in stdout.splitlines() if line.strip()])
    return {"present": count > 0, "count": count}


# Fields requested from `gh pr list --json` for hygiene checks. gh rejects the
# entire call on a single unknown field and this wrapper reports that as
# {"available": False}, so a typo here degrades silently — the same failure mode
# as #6749. Validated against gh's real field set in tests/test_github_api.py.
HYGIENE_PR_FIELDS = ("number", "title", "author", "createdAt")


def _hygiene_open_pr_drift(repo_dir: Path) -> dict:
    """Detect open PRs older than 24h authored by *[bot] identities.

    Uses gh CLI. If gh is unavailable or auth fails, returns available=False.
    """
    gh_bin = shutil.which("gh")
    if not gh_bin:
        return {"available": False, "present": False, "prs": [], "reason": "gh not found"}

    try:
        # Try to get the remote URL to determine owner/repo
        remote_stdout, rc = _run_git(repo_dir, ["remote", "get-url", "origin"])
        if rc != 0 or not remote_stdout.strip():
            return {"available": False, "present": False, "prs": [], "reason": "no git remote origin"}
        remote_url = remote_stdout.strip()
        # Parse owner/repo from remote URL (https or ssh)
        import re
        m = re.search(r"[:/]([^/]+/[^/.]+?)(?:\.git)?$", remote_url)
        if not m:
            return {"available": False, "present": False, "prs": [], "reason": f"cannot parse remote: {remote_url}"}
        repo_slug = m.group(1)

        result = subprocess.run(
            [
                gh_bin, "pr", "list",
                "--repo", repo_slug,
                "--state", "open",
                "--json", ",".join(HYGIENE_PR_FIELDS),
                "--limit", "50",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            reason = result.stderr.strip() or "gh returned non-zero"
            return {"available": False, "present": False, "prs": [], "reason": reason}

        prs_raw = json.loads(result.stdout)
        from datetime import timezone
        now = datetime.now(timezone.utc)
        bot_old_prs = []
        for pr in prs_raw:
            author = pr.get("author", {}).get("login", "")
            if not author.endswith("[bot]"):
                continue
            created_at_str = pr.get("createdAt", "")
            try:
                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                age_hours = (now - created_at).total_seconds() / 3600
            except (ValueError, TypeError):
                age_hours = 0
            if age_hours > 24:
                bot_old_prs.append({
                    "number": pr["number"],
                    "title": pr["title"],
                    "age_hours": round(age_hours, 1),
                    "author": author,
                })

        return {"available": True, "present": bool(bot_old_prs), "prs": bot_old_prs}

    except subprocess.TimeoutExpired:
        return {"available": False, "present": False, "prs": [], "reason": "gh timed out"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "present": False, "prs": [], "reason": str(exc)}


def _hygiene_stale_progress_md(repo_dir: Path) -> dict:
    """Detect a PROGRESS.md that is older than 7 days AND the repo has uncommitted work."""
    from datetime import timezone
    progress_path = repo_dir / "PROGRESS.md"
    if not progress_path.exists():
        return {"present": False, "age_days": None}

    # Check age
    mtime = progress_path.stat().st_mtime
    now_ts = datetime.now(timezone.utc).timestamp()
    age_days = (now_ts - mtime) / 86400
    if age_days <= 7:
        return {"present": False, "age_days": round(age_days, 1)}

    # Check if repo has uncommitted work OTHER THAN PROGRESS.md itself.
    # PROGRESS.md is the documented always-dirty file (see project-tracker
    # CLAUDE.md), so a repo whose only dirty path is PROGRESS.md is not
    # "claiming progress without backing" — it has no claim either way.
    non_progress_dirty = _hygiene_non_progress_dirty_files(repo_dir)
    if not non_progress_dirty:
        return {"present": False, "age_days": round(age_days, 1)}

    return {"present": True, "age_days": round(age_days, 1)}


@cli.command()
@click.option("--port", default=8000, type=int, show_default=True, help="Port to run on")
@click.option("--no-scan", is_flag=True, help="Skip the initial project scan on launch")
@click.option("--reload", is_flag=True, help="Enable auto-reload for development")
def launch(port, no_scan, reload):
    """Launch the web dashboard."""
    console.print("[bold green]Launching Project Tracker Dashboard...[/bold green]\n")
    init_db()
    if not no_scan:
        console.print("[dim]Running quick scan (graph rebuild deferred to background)...[/dim]")
        _scan_impl(no_graph=True)
    else:
        console.print("[yellow]Skipping initial scan. Using existing data.[/yellow]")
    dashboard_path = Path(__file__).parent.parent / "dashboard" / "app.py"
    if not dashboard_path.exists():
        console.print("[red]Error: Dashboard not found. Check installation.[/red]")
        return
    url = f"http://localhost:{port}"
    console.print(f"\n[bold green]✅ Dashboard starting at {url}[/bold green]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")
    import threading
    def open_browser():
        time.sleep(2)
        webbrowser.open(url)
    threading.Thread(target=open_browser, daemon=True).start()
    project_root = Path(__file__).parent.parent
    venv_python = _resolve_dashboard_python(project_root)
    bind_host = os.getenv("PT_DASHBOARD_HOST", "127.0.0.1")
    uvicorn_cmd = [str(venv_python), "-m", "uvicorn", "dashboard.app:app", "--host", bind_host, "--port", str(port)]
    if reload: uvicorn_cmd.append("--reload")
    # The dashboard needs no secrets, so it runs without Doppler.
    try:
        subprocess.run(uvicorn_cmd, cwd=project_root)
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Dashboard stopped[/yellow]")


@cli.group(name="project")
def project_group():
    """Manage project records and which machine a project is checked out to.

    \b
    Checkouts: a project checked out to one machine (the MacBook or the
    Mac Mini) is developed only there, for as long as it stays out, which
    can be weeks. Before development work, check `pt project checkouts`.
    Cards can still be added to a checked-out project: the board is how you
    reach the floor manager working it. `pt list`, `pt status`, `pt tasks`
    (and its --board and --json), `pt tasks next`, `pt tasks show` and the
    dashboard's board, cards, task details and project filter all show it.
      pt project checkout my-app --machine mini --holder "Qwen on Mini"
      pt project checkin my-app --by "Claude on MacBook"
    A checkout taken for one card names it with --card; once that card is
    Done or idle for a few days, `pt tasks done` and `pt tasks` remind you to
    check it back in. Leave --card off only for a project that lives there.
    """
    pass


@project_group.command(name="add")
@click.argument("project_id")
@click.option("--name", default=None, help="Display name. Defaults to PROJECT_ID.")
@click.option("--path", "project_path", default=None, help="Existing project directory for non-virtual projects.")
@click.option("--virtual", is_flag=True, help="Create a board-only project with no filesystem directory.")
@click.option("--description", default=None, help="Project description.")
@click.option("--status", default="active", help="Project status.")
def project_add(project_id, name, project_path, virtual, description, status):
    """Add a project record without running a filesystem scan.

    Virtual projects are board-only records. They intentionally use a
    virtual:// path and do not create a directory under PROJECTS_ROOT.
    """
    cleaned_id = project_id.strip()
    allowed_slug_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not cleaned_id or any(ch not in allowed_slug_chars for ch in cleaned_id):
        raise click.ClickException("Project ID must be a simple slug, e.g. openclaw")

    if virtual and project_path:
        raise click.ClickException("Use either --virtual or --path, not both")

    if not virtual and not project_path:
        raise click.ClickException("Provide --virtual for board-only projects or --path for real directories")

    if virtual:
        resolved_path = f"virtual://{cleaned_id}"
        project_type = "virtual"
    else:
        path_obj = Path(project_path).expanduser().resolve()
        if not path_obj.is_dir():
            raise click.ClickException(f"Project path does not exist or is not a directory: {path_obj}")
        resolved_path = str(path_obj)
        project_type = "standard"

    db = DatabaseManager()
    if db.get_project(cleaned_id):
        raise click.ClickException(f"Project '{cleaned_id}' already exists")

    display_name = name.strip() if name and name.strip() else cleaned_id
    now = datetime.now().isoformat()
    db.add_project(
        project_id=cleaned_id,
        name=display_name,
        path=resolved_path,
        status=status,
        description=description,
        last_modified=now,
        project_type=project_type,
    )

    console.print(
        f"[green]✅ Added {'virtual ' if virtual else ''}project '{display_name}' "
        f"({cleaned_id})[/green]"
    )
    console.print(f"[dim]Path: {resolved_path}[/dim]")


@project_group.command(name="checkout")
@click.argument("project")
@click.option("--machine", required=True, type=click.Choice(sorted(MACHINE_LABELS)),
              help="laptop (the MacBook) or mini (the Mac Mini)")
@click.option("--holder", required=True, help='Who has it, e.g. "Qwen on Mini"')
@click.option("--note", default=None, help="Optional: what it is out for")
@click.option("--card", type=click.IntRange(min=1, max=2**63 - 1), default=None,
              help="The card it is out for. Without it the checkout is owned and never gets a check-in reminder.")
def project_checkout(project, machine, holder, note, card):
    """Check PROJECT out to a machine; refused while someone else has it.

    It stays out until `pt project checkin`. There is no expiry. With --card,
    `pt tasks done` on that card and `pt tasks` remind you to check it in.
    """
    db = DatabaseManager()
    project_id = _resolve_project_id(db, project)
    if not project_id:
        raise click.ClickException(f"Project '{project}' not found")
    task_id = None
    if card is not None:
        task_id = db.resolve_task_id(card)
        if task_id is None:
            raise click.ClickException(f"Card #{card} not found")
    try:
        checkout = db.checkout_project(project_id, machine, holder, note, task_id=task_id)
    except ProjectCheckedOut as exc:
        raise click.ClickException(f"refused: {exc}") from exc
    except ValueError as exc:
        # Migration 023/024 pending, an empty --holder, or the project or card was just deleted.
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{project_id}: {_checkout_label(checkout)}")


@project_group.command(name="checkin")
@click.argument("project")
@click.option("--by", "by", required=True, help='Who is checking it in, e.g. "Claude on MacBook"')
@click.option(
    "--checkout-id",
    type=click.IntRange(min=1, max=2**63 - 1),
    help="Only check in this exact checkout record (prevents releasing a replacement checkout).",
)
def project_checkin(project, by, checkout_id):
    """Check PROJECT back in, recording who and when."""
    db = DatabaseManager()
    project_id = _resolve_project_id(db, project)
    if not project_id:
        raise click.ClickException(f"Project '{project}' not found")
    try:
        checkout = db.checkin_project(project_id, by, checkout_id)
    except CheckoutsMigrationPendingError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--by") from exc
    if checkout is None:
        suffix = f" with checkout id {checkout_id}" if checkout_id is not None else ""
        raise click.ClickException(f"{project_id} is not checked out{suffix}")
    click.echo(
        f"{project_id} checked in by {checkout['checked_in_by']} "
        f"(was out to {MACHINE_LABELS[checkout['machine']]} with {checkout['holder']})"
    )


@project_group.command(name="checkouts")
def project_checkouts():
    """List every project that is checked out, and to which machine."""
    checkouts = DatabaseManager().get_open_checkouts()
    if not checkouts:
        click.echo("No projects are checked out.")
    for project_id, checkout in sorted(checkouts.items()):
        click.echo(f"{project_id}: {_checkout_label(checkout)}")
        if checkout["note"]:
            click.echo(f"    {checkout['note']}")


def _scan_portfolio_for_references(project_id: str, project_name: str, projects_root: Path) -> tuple[list[tuple[Path, int, str]], list[tuple[Path, str]]]:
    """Scan portfolio markdown/yaml/json for stale references to a project.

    Returns (hits, scan_errors) where:
      - hits: list of (path, line_number, matched_line) tuples
      - scan_errors: list of (path, error_message) for paths the scan couldn't read

    Read-only — never mutates files. Used by retire-project to print a
    manual-cleanup report for the human.
    """
    import logging
    _log = logging.getLogger(__name__)
    hits: list[tuple[Path, int, str]] = []
    scan_errors: list[tuple[Path, str]] = []
    needles = {project_id.lower()}
    if project_name and project_name.lower() != project_id.lower():
        needles.add(project_name.lower())
    exts = {".md", ".yaml", ".yml", ".json"}
    skip_dirs = {".git", "node_modules", "venv", ".venv", "__pycache__", "data", "backups", "_archive"}
    for root, dirs, files in os.walk(projects_root):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        root_path = Path(root)
        # Skip the project being retired itself
        try:
            if root_path.resolve().is_relative_to((projects_root / project_id).resolve()):
                continue
        except (ValueError, OSError) as e:
            _log.warning(f"retire-project: could not resolve path {root_path} for skip check: {e}")
            scan_errors.append((root_path, f"resolve: {e}"))
        for f in files:
            if Path(f).suffix.lower() not in exts:
                continue
            fp = root_path / f
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, 1):
                        lowered = line.lower()
                        if any(n in lowered for n in needles):
                            hits.append((fp, lineno, line.rstrip()))
                            if sum(1 for h in hits if h[0] == fp) >= 10:
                                break
            except (OSError, UnicodeDecodeError) as e:
                _log.warning(f"retire-project: could not scan {fp}: {e}")
                scan_errors.append((fp, str(e)))
                continue
    return hits, scan_errors


@cli.command(name="retire-project")
@click.argument("project")
@click.option("--execute", is_flag=True, help="Actually retire. Without this, runs in dry-run mode.")
@click.option("--keep-files", is_flag=True, help="Skip sending the project directory to Trash.")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def retire_project(project, execute, keep_files, yes):
    """Retire a project: trash the directory, cascade-delete DB rows, and audit stale refs.

    DRY-RUN BY DEFAULT. Prints the plan and the stale-reference audit without
    touching anything. Pass --execute to actually perform the retirement.

    What retire does:
      1. Sends the project directory to Trash (via send2trash; never rm)
      2. Deletes the project row and cascade-deletes tasks, cron jobs, agents,
         service dependencies and checkouts via db.delete_project()
      3. Prints a report of stale references to this project found in other
         projects' markdown/yaml/json (for manual review — retire never
         auto-edits files outside the retired project).

    Use --keep-files to retire from the DB only, leaving the directory intact.
    """
    from send2trash import send2trash

    db = DatabaseManager()
    project_id = _resolve_project_id(db, project)
    target = db.get_project(project_id) if project_id else None
    if not target:
        console.print(f"[red]Project '{project}' not found in database.[/red]")
        sys.exit(1)

    project_id = target["id"]
    project_name = target["name"]
    project_path = Path(target.get("path") or "")

    # Retirement removes every task, archived ones included — count them all.
    tasks = db.get_tasks(project_id=project_id, include_archived=True)
    cron_jobs = db.get_cron_jobs(project_id)
    agents = db.get_ai_agents(project_id)

    mode = "[bold yellow]DRY RUN[/bold yellow]" if not execute else "[bold red]EXECUTE[/bold red]"
    console.print(f"\n{mode} [bold]retire-project[/bold] — {project_name} ({project_id})")
    console.print(f"[bold]Path:[/bold]     {project_path}")
    path_ok = project_path.exists() if str(project_path) else False
    console.print(f"[bold]On disk:[/bold]  {'yes' if path_ok else 'no'}")
    console.print(f"[bold]Tasks:[/bold]    {len(tasks)} (cascade-delete)")
    console.print(f"[bold]Crons:[/bold]    {len(cron_jobs)} (cascade-delete)")
    console.print(f"[bold]Agents:[/bold]   {len(agents)} (cascade-delete)")
    if keep_files:
        console.print(f"[bold]Files:[/bold]    [yellow]--keep-files set; directory will remain on disk[/yellow]")
    else:
        console.print(f"[bold]Files:[/bold]    directory → Trash via send2trash")

    console.print("\n[bold]Stale reference audit[/bold] (read-only scan of portfolio):")
    refs, scan_errors = _scan_portfolio_for_references(project_id, project_name, PROJECTS_BASE_DIR)
    if scan_errors:
        console.print(f"  [yellow]⚠ {len(scan_errors)} path(s) could not be scanned (see log for details):[/yellow]")
        for err_path, err_msg in scan_errors[:5]:
            rel = err_path.relative_to(PROJECTS_BASE_DIR) if err_path.is_relative_to(PROJECTS_BASE_DIR) else err_path
            console.print(f"    [yellow]{rel}: {err_msg}[/yellow]")
        if len(scan_errors) > 5:
            console.print(f"    [dim]... and {len(scan_errors) - 5} more[/dim]")
    if not refs:
        console.print("  [green]no references found[/green]" + (" [dim](plus unreadable paths above)[/dim]" if scan_errors else ""))
    else:
        by_file: dict[Path, list[tuple[int, str]]] = {}
        for fp, lineno, line in refs:
            by_file.setdefault(fp, []).append((lineno, line))
        for fp, lines in sorted(by_file.items()):
            rel = fp.relative_to(PROJECTS_BASE_DIR) if fp.is_relative_to(PROJECTS_BASE_DIR) else fp
            console.print(f"  [cyan]{rel}[/cyan] ({len(lines)} hit{'s' if len(lines) != 1 else ''})")
            for lineno, line in lines[:3]:
                console.print(f"    [dim]{lineno}:[/dim] {line[:100]}")
            if len(lines) > 3:
                console.print(f"    [dim]... and {len(lines) - 3} more[/dim]")
        console.print("[yellow]Review the above manually after retire — retire does not edit other projects' files.[/yellow]")

    if not execute:
        console.print("\n[dim]Dry run complete. Re-run with --execute to retire for real.[/dim]")
        return

    if not yes:
        console.print(f"\n[bold red]⚠️  DESTRUCTIVE OPERATION[/bold red]")
        confirm = click.prompt(
            f"Type the project name '{project_name}' to confirm retirement",
            default="",
            show_default=False,
        )
        if confirm.lower() != project_name.lower():
            console.print("[yellow]Cancelled. Project name did not match.[/yellow]")
            sys.exit(1)

    # Verify the database backup and obtain authorization BEFORE moving files.
    # A rejected grant must leave both the project directory and its rows intact.
    delete_project = db.prepare_operation(
        "delete_project", reason=f"Confirmed retire-project {project_id}",
        project_id=project_id,
    )

    # 1. Send directory to Trash (unless --keep-files)
    if not keep_files and path_ok:
        try:
            # Highest-consequence delete in this codebase — it trashes a whole
            # project directory, the shape of the incident that trashed a
            # project its developers were working in. Record it before it happens (#6890).
            _audit_destructive("retire-project", project_path, project_path.parent)
            send2trash(str(project_path))
            console.print(f"[green]✓[/green] sent {project_path} to Trash")
        except Exception as e:
            console.print(f"[red]Failed to trash {project_path}: {e}[/red]")
            console.print("[yellow]Aborting before DB delete — directory is still on disk.[/yellow]")
            sys.exit(1)
    elif not keep_files and not path_ok:
        console.print(f"[dim]✓ directory already missing, nothing to trash[/dim]")

    # 2. Cascade-delete DB rows
    delete_project()
    console.print(f"[green]✓[/green] deleted project '{project_name}' and cascaded rows from the database")

    console.print(f"\n[bold green]✅ Retired '{project_name}'.[/bold green]")
    if refs:
        console.print(f"[yellow]Remember: {len(refs)} stale reference(s) in {len({r[0] for r in refs})} file(s) still need manual review.[/yellow]")


@cli.command(name="help", hidden=True)
@click.argument("command", nargs=-1)
@click.pass_context
def help_command(ctx, command):
    """Show click's own --help for pt, or for `pt help COMMAND [SUBCOMMAND...]`."""
    target = ctx.parent
    for name in command:
        sub = target.command.get_command(target, name) if isinstance(target.command, click.Group) else None
        if sub is None:
            raise click.UsageError(f"No such command '{name}'.", ctx=target)
        target = click.Context(sub, info_name=name, parent=target)
    click.echo(target.get_help())


# =============================================================================
# Backup group
# =============================================================================

@click.group(name="backup", invoke_without_command=True)
@click.pass_context
def backup_group(ctx):
    """Inspect backup health and restore from full database snapshots."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@backup_group.command(name="status")
@click.option("--json", "json_output", is_flag=True, help="Output status as JSON")
def backup_status(json_output: bool):
    """Show local and off-machine backup health for project-tracker."""
    status = get_backup_status()
    if json_output:
        click.echo(json.dumps(status, indent=2))
        return

    color = status.get("status_color", "blue")
    console.print(f"[bold]Backup Status:[/bold] [{color}]{status['status']}[/]")
    click.echo(status["message"])

    local = status["local_full"]
    click.echo(
        "Local full backup: "
        f"{local['message']} "
        f"(count: {local['count']}, path: {local['path'] or 'none'})"
    )

    cloud = status["cloud"]
    cloud_suffix = f" target: {cloud['configured_dest']}" if cloud["configured_dest"] else ""
    click.echo(f"Off-machine copy: {cloud['message']}{cloud_suffix}")

    launch_agent = status["launch_agent"]
    if launch_agent["present"]:
        schedule = launch_agent.get("schedule") or "configured"
        click.echo(f"LaunchAgent: installed ({schedule}) at {launch_agent['path']}")
    else:
        click.echo(f"LaunchAgent: not found at {launch_agent['path']}")

    log = status["log"]
    if log["exists"]:
        click.echo(
            "Backup log: "
            f"last local success {log['last_local_success_age_human'] or 'unknown'} "
            f"({log['path']})"
        )
        if log["last_cloud_failure_at"] and log["last_cloud_failure_at"] != log["last_cloud_success_at"]:
            click.echo(
                "Last cloud-copy failure: "
                f"{log['last_cloud_failure_at']}"
                + (f" ({log['last_cloud_failure_detail']})" if log["last_cloud_failure_detail"] else "")
            )
    else:
        click.echo(f"Backup log: missing ({log['path']})")

    if status["remotes"]:
        click.echo(f"Rclone remotes: {', '.join(status['remotes'])}")


@backup_group.command(name="list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def backup_list(json_output: bool):
    """List restorable snapshots by name."""
    try:
        rows = DatabaseManager().backup_list()
    except _LOCAL_DB_ERRORS as err:
        console.print(f"[red]pt backup list: {err}[/red]")
        raise SystemExit(2)

    if json_output:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("No backups found.")
        return
    for row in rows:
        size_mb = row["size_bytes"] / (1024 * 1024)
        click.echo(f"{row['modified']}  {size_mb:8.1f} MB  {row['name']}")


@backup_group.command(name="create")
def backup_create():
    """Take a verified timestamped snapshot to both backup locations."""
    try:
        result = DatabaseManager().backup_create()
    except _LOCAL_DB_ERRORS as err:
        console.print(f"[red]pt backup create: {err}[/red]")
        raise SystemExit(2)
    size_mb = result["size_bytes"] / (1024 * 1024)
    console.print(f"[green]✓ backup {Path(result['path']).name} ({size_mb:.1f} MB), verified[/green]")


@backup_group.command(name="prune")
@click.option("--dry-run", is_flag=True, help="List what would move; move nothing.")
def backup_prune(dry_run):
    """Move old backups to the Trash.

    In each backup location every kind of backup (its name before the first
    timestamp) keeps its newest 10 (scheduled snapshots) or 5 (every other
    kind, such as the pre-destructive copies). The scheduled backup job runs
    this after the snapshot and the offsite copy. Exits 1 if anything could
    not be moved or listed.
    """
    def one_line(text):
        # The scheduled job logs this output as one `|`-separated field.
        return " ".join(str(text).split()).replace("|", "/")

    try:
        result = DatabaseManager().backup_prune(dry_run=dry_run)
    except _LOCAL_DB_ERRORS as err:
        click.echo(one_line(f"pt backup prune: {err}"))
        raise SystemExit(2)
    size_mb = result["size_bytes"] / (1024 * 1024)
    if dry_run:
        for name in result["pruned"]:
            click.echo(name)
    summary = f"{'would move' if dry_run else 'moved'} {len(result['pruned'])} file(s), {size_mb:.1f} MB"
    if result["errors"]:
        summary += f"; failed for {len(result['errors'])}, left in place: {result['errors'][0]}"
    click.echo(one_line(summary))
    if result["errors"]:
        raise SystemExit(1)


@backup_group.command(name="offsite")
@click.argument("backup_name", required=False)
def backup_offsite(backup_name):
    """Copy a verified snapshot off-machine using rclone.

    Settings come from the environment, else from the backup LaunchAgent's
    EnvironmentVariables: PT_BACKUP_RCLONE_DEST is the destination (required),
    and RCLONE_CONFIG (alias RCLONE_CONFIG_PATH) names the rclone config file,
    default ~/.config/rclone/rclone.conf, which must exist.
    """
    try:
        db = DatabaseManager()
        result = db.backup_offsite_copy(name=backup_name) if backup_name else db.backup_offsite_copy()
    except (*_LOCAL_DB_ERRORS, subprocess.TimeoutExpired) as err:
        try:
            from scripts.discovery.backup_reader import append_cloud_copy_log

            append_cloud_copy_log(ok=False, detail=str(err))
        except Exception:  # governance: allow-silent SF001: the copy failure itself is printed and exits 2 on the next lines; the status log is secondary, and backup status still goes stale by age
            pass
        console.print(f"[red]pt backup offsite: {err}[/red]")
        raise SystemExit(2)
    size_mb = result["size_bytes"] / (1024 * 1024)
    detail = f"{result['name']} → {result['dest']}"
    try:
        from scripts.discovery.backup_reader import append_cloud_copy_log

        append_cloud_copy_log(ok=True, detail=detail)
    except Exception as err:  # noqa: BLE001
        # Status log is best-effort; never hide a successful copy. But say
        # so: without this entry `pt backup status` and the dashboard keep
        # reporting the previous copy as the latest.
        console.print(f"[yellow]⚠ could not record the copy in the backup status log: {err}[/yellow]")
    console.print(
        f"[green]✓ offsite {result['name']} ({size_mb:.1f} MB) → {result['dest']}[/green]"
    )


@backup_group.command(name="restore")
@click.argument("backup_name")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def backup_restore(backup_name: str, yes: bool):
    """Restore tracker.db from a named snapshot.

    \b
    BREAKING CHANGE: this used to take a filesystem path and copy whatever was
    at it over the live database — a write-anything primitive that needed no
    SQL and no handle on tracker.db. It now takes a NAME, which the database
    tool resolves inside its configured backup directories. Run
    `pt backup list` to see the names.
    """
    if not yes:
        confirm = click.confirm(
            f"Restore the tracker database from '{backup_name}'? This replaces the live DB.",
            default=False,
        )
        if not confirm:
            click.echo("Cancelled")
            return

    try:
        result = DatabaseManager().authorize(
            "backup_restore",
            reason=f"pt backup restore from snapshot {backup_name}",
            name=backup_name,
        )
    except _LOCAL_DB_ERRORS as err:
        console.print(f"[red]pt backup restore: {err}[/red]")
        raise SystemExit(2)

    console.print(f"[green]Restored database from {result['restored_from']}[/green]")
    if result["trashed_sidecars"]:
        click.echo("Trashed stale sidecars:")
        for sidecar in result["trashed_sidecars"]:
            click.echo(f"  - {sidecar}")


# =============================================================================
# Tasks group
# =============================================================================

def _tasks_list_options(f):
    """The one option set shared by `pt tasks` and `pt tasks list`."""
    for option in reversed([
        click.option("-p", "--project", default=None, help="Filter by project name or ID"),
        click.option("-s", "--status", default=None, help="Filter by status (Backlog, To Do, In Progress, Review, Done)"),
        click.option("-a", "--all", "show_all", is_flag=True, help="Include completed tasks"),
        click.option("--board", is_flag=True, help="Show columnar Kanban board view"),
        click.option("--json", "json_output", is_flag=True, help="Output as JSON"),
        click.option("--needs-prompt", is_flag=True, help="Show only tasks without prompts"),
        click.option("--ready", is_flag=True, help="Show To Do tasks with complete prompts (ready to start)"),
        click.option("--proposals", is_flag=True, help="Show only proposal tasks"),
        click.option("--archived", is_flag=True, help="Show only archived Done cards (hidden from the board, never deleted)"),
    ]):
        f = option(f)
    return f


def _tasks_list_impl(project, status, show_all, board, json_output, needs_prompt, ready, proposals, archived):
    """List tasks. Both `pt tasks` and `pt tasks list` run exactly this."""
    db = DatabaseManager()
    if project:
        project_id = _resolve_project_id(db, project)
        project_label = project
        if not project_id:
            console.print(f"[red]Project '{project}' not found[/red]"); return
    else:
        project_id = _detect_project_from_cwd(db)
        project_label = None
        if project_id:
            detected = db.get_project(project_id)
            project_label = detected["name"] if detected else None
    task_list = db.get_tasks(project_id=project_id, status=status, include_archived=archived)
    if archived:
        # --archived is a retention view: only the Done cards the per-project
        # display cap has hidden. They still exist and are never deleted.
        task_list = [t for t in task_list if t.get("archived_at")]
    elif proposals:
        task_list = [t for t in task_list if t.get("task_type") == "proposal"]
    else:
        # Hide proposals from default listing unless --all is used
        if not show_all:
            task_list = [t for t in task_list if t.get("task_type") != "proposal"]
        if not show_all and not status:
            task_list = [t for t in task_list if t["status"] not in ("Done", "Cancelled")]
    if needs_prompt:
        task_list = [t for t in task_list if not t.get("prompt")]
    if ready:
        task_list = [t for t in task_list if t["status"] == "To Do" and _has_complete_prompt(t.get("prompt"))]
    checkouts = db.get_open_checkouts()
    if not json_output:
        _print_checkin_reminders(db, db.checkin_reminders(project_id))
    if project_id in checkouts and not json_output:
        console.print(f"[bold red]{escape(_checkout_label(checkouts[project_id]))}[/bold red]")
        console.print(
            f"[red]Develop it only on the {MACHINE_LABELS[checkouts[project_id]['machine']]}. "
            "Cards added here reach the floor manager working it there.[/red]\n"
        )
    if board and not json_output:
        statuses = ["Backlog", "To Do", "In Progress", "Review", "Done"]
        table = Table(show_header=True, header_style="bold")
        for col in statuses: table.add_column(col, width=20)
        def format_task(task):
            text = task["text"]
            if len(text) > 18: text = text[:17] + "..."
            # A project-scoped board has the banner above; the all-projects board marks each card.
            out_marker = "" if project_id else escape(_checkout_marker(checkouts, task["project_id"]))
            label = f"{out_marker}#{task['id']} {text}"
            priority = task.get("priority")
            color = {"Critical": "red", "High": "red", "Medium": "yellow", "Low": "green"}.get(priority)
            return f"[{color}]{label}[/]" if color else label
        by_status = {s: [] for s in statuses}
        for task in task_list: by_status.get(task["status"], []).append(task)
        max_rows = max((len(by_status[s]) for s in statuses), default=0)
        max_rows = min(max_rows, 10)
        for i in range(max_rows):
            row = []
            for status_name in statuses:
                tasks = by_status[status_name]
                row.append(format_task(tasks[i]) if i < len(tasks) else "")
            table.add_row(*row)
        console.print(table)
        return
    _display_tasks(task_list, project_label, json_output=json_output, db=db, checkouts=checkouts)


@click.group(name="tasks", invoke_without_command=True)
@click.pass_context
@_tasks_list_options
def tasks_group(ctx, project, status, show_all, board, json_output, needs_prompt, ready, proposals, archived):
    """Manage Kanban board tasks.

    Bare `pt tasks` and `pt tasks list` are the same command with the same
    options.

    \b
    Examples:
        pt tasks                       # Show open tasks (all projects)
        pt tasks -p project-tracker    # Tasks for a specific project
        pt tasks -s "In Progress"      # Filter by status
        pt tasks --all                 # Include completed tasks
        pt tasks --archived            # Show archived Done cards
        pt tasks --proposals           # Show only proposals
        pt tasks --board               # Columnar Kanban view
        pt tasks create "Fix bug" -p myproject -d "- [ ] pytest tests/test_foo.py passes"

    """
    if ctx.invoked_subcommand is not None: return
    _tasks_list_impl(project, status, show_all, board, json_output, needs_prompt, ready, proposals, archived)


@tasks_group.command(name="list")
@_tasks_list_options
def tasks_list(project, status, show_all, board, json_output, needs_prompt, ready, proposals, archived):
    """List tasks from the Kanban board (same as bare `pt tasks`)."""
    _tasks_list_impl(project, status, show_all, board, json_output, needs_prompt, ready, proposals, archived)


@tasks_group.command(name="create")
@click.argument("text")
@click.option("-p", "--project", default=None, help="Project ID or name (auto-detects from cwd)")
@click.option("-s", "--status", default="Backlog", help="Initial status")
@click.option("--priority", default=None, help="Priority: Critical, High, Medium, Low")
@click.option("--prompt", default=None, help="Agent prompt (execution instructions for AI)")
@click.option("--category", default=None, help="Task category label (none if omitted)")
@click.option("-d", "--description", default=None, help="Acceptance criteria (required) plus any rich description — stored in notes field. Must include at least one '- [ ] <how completion is verified>' checklist line.")
@click.option("--parent", type=int, default=None, help="Parent task ID (creates subtask)")
@click.option("--blocked-by", default=None, help="Comma-separated task IDs that block this task")
@click.option("--proposal", is_flag=True, help="Create as a proposal (hidden from default listing until approved)")
@click.option("--created-by", "created_by", default=None, help="Origin label (defaults to cwd-derived: 'architect', project name, or 'unknown')")
def tasks_create(text, project, status, priority, prompt, category, description, parent, blocked_by, proposal, created_by):
    """Create a new task.

    Auto-detects project from current directory.

    \b
    Acceptance criteria (required, #7608): -d/--description must include at
    least one '- [ ] <how completion is verified>' checklist line — a
    heading alone doesn't count. Subtasks and --proposal cards are not
    exempt; for a proposal, state the decision/approval criterion instead,
    e.g. '- [ ] Approve if X; reject if Y'.

        pt tasks create "Fix flaky retry" -p myproject \\
            -d "- [ ] pytest tests/test_retry.py passes"
    """
    db = DatabaseManager()
    if project: project_id = _resolve_project_id(db, project)
    else: project_id = _detect_project_from_cwd(db)
    if not project_id:
        if project: console.print(f"[red]Project '{project}' not found[/red]")
        else: console.print("[red]Could not auto-detect project from current directory. Use -p to specify.[/red]")
        return
    valid_statuses = ["Backlog", "To Do", "In Progress", "Review", "Done", "Cancelled"]
    if status not in valid_statuses:
        console.print(f"[red]Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}[/red]"); return
    if priority and priority not in ["Critical", "High", "Medium", "Low"]:
        console.print(f"[red]Invalid priority '{priority}'. Must be one of: Critical, High, Medium, Low[/red]"); return
    blocked_by_json = None
    if blocked_by:
        blocked_by_json = _parse_blocked_by(db, blocked_by)
    WORKFLOW_FOOTER = "\n---\n\n## Workflow Protocol\n- [ ] Start: `pt tasks start <id>`\n- [ ] Complete work\n- [ ] Open PR: `pt tasks review <id>`\n- [ ] PR merges: `pt tasks done <id>`"
    try:
        final_prompt = prompt
        if prompt: final_prompt = prompt.rstrip() + WORKFLOW_FOOTER
        task_type = "proposal" if proposal else None
        resolved_created_by = created_by if created_by else _resolve_created_by()
        task = db.create_card(text=text, project_id=project_id, status=status, priority=priority, prompt=final_prompt, task_type=task_type, category=category, parent_id=parent, blocked_by=blocked_by_json, notes=description, created_by=resolved_created_by)
        msg = f"[green]Created {'proposal' if proposal else 'task'} #{task['id']}: {text[:50]}{'...' if len(text) > 50 else ''}[/green]"
        if description: msg += f" [dim](+ description)[/dim]"
        if parent: msg += f" [dim](subtask of #{parent})[/dim]"
        if blocked_by: msg += f" [dim](blocked by {blocked_by})[/dim]"
        console.print(msg)
    except BlockedTaskProjectError:
        console.print(
            f"[yellow]Failed to create task: task creation is blocked for '{project_id}'.[/yellow]"
        )
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Failed to create task: {e}[/red]")
        sys.exit(1)


@tasks_group.command(name="update")
@click.argument("task_id", type=int)
@click.option("-s", "--status", default=None, help="New status")
@click.option("-t", "--text", default=None, help="New task text")
@click.option("--priority", default=None, help="New priority")
@click.option("--prompt", default=None, help="Agent prompt (execution instructions for AI)")
@click.option("--review-comment", default=None, help="Reviewer feedback")
@click.option("--notes", default=None, help="Replace the card's notes outright; refuses to overwrite existing non-empty notes unless --replace-notes is also passed")
@click.option("--append-notes", default=None, help="Append a timestamped entry to the card's existing notes (never removes text)")
@click.option("--replace-notes", is_flag=True, help="Allow --notes to overwrite existing non-empty notes")
@click.option("--blocked-by", default=None, help="Comma-separated task IDs (empty string clears)")
def tasks_update(task_id, status, text, priority, prompt, review_comment, notes, blocked_by, append_notes, replace_notes):
    """Update an existing task.

    Notes are never silently replaced: --append-notes adds a stamped entry to
    whatever is already there, while --notes only replaces the whole field
    when the card has no notes yet or --replace-notes is passed explicitly.
    """
    if notes is not None and append_notes is not None:
        console.print("[red]--notes and --append-notes are mutually exclusive; pass one or the other.[/red]")
        sys.exit(1)
    if replace_notes and notes is None:
        console.print("[red]--replace-notes requires --notes.[/red]")
        sys.exit(1)

    db = DatabaseManager()
    updates = {}
    if status:
        valid_statuses = ["Backlog", "To Do", "In Progress", "Review", "Done", "Cancelled"]
        if status not in valid_statuses:
            console.print(f"[red]Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}[/red]"); sys.exit(1)
        updates["status"] = status
    if text is not None: updates["text"] = text
    if priority:
        if priority not in ["Critical", "High", "Medium", "Low"]:
            console.print(f"[red]Invalid priority '{priority}'[/red]"); sys.exit(1)
        updates["priority"] = priority
    if prompt is not None: updates["prompt"] = prompt
    if review_comment is not None: updates["review_comment"] = review_comment
    if blocked_by is not None:
        if blocked_by == "": updates["blocked_by"] = None
        else: updates["blocked_by"] = _parse_blocked_by(db, blocked_by)
    if not updates and notes is None and append_notes is None:
        console.print("[yellow]No updates specified. Use -s, -t, --priority, --prompt, --notes, --append-notes, --replace-notes, or --blocked-by.[/yellow]"); return

    task_id = _resolve_task_id(db, task_id)
    task = db.get_task(task_id)
    if not task:
        console.print(f"[red]Task #{task_id} not found[/red]"); sys.exit(1)

    if append_notes is not None:
        existing = task.get("notes")
        base = (existing or "").rstrip()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{stamp}] {append_notes}"
        updates["notes"] = f"{base}\n\n{entry}" if base else entry
    elif notes is not None:
        existing_notes = task.get("notes")
        if (existing_notes or "").strip() and not replace_notes:
            console.print(
                "[red]Refusing to replace the existing notes on task "
                f"#{task_id}. Use --append-notes to add to them, or pass "
                "--replace-notes with --notes to overwrite.[/red]"
            )
            sys.exit(1)
        updates["notes"] = notes

    try:
        db.update_task(task_id, notes_source="cli", **updates)
    except Exception as e:
        console.print(f"[red]Failed to update task #{task_id}: {e}[/red]")
        sys.exit(1)

    console.print(f"[green]Updated task #{task_id}[/green]")
    for key, value in updates.items():
        if key == "notes" and append_notes is not None:
            value = entry
        console.print(f"  {key}: {escape(str(value))}")


@tasks_group.command(name="notes-history")
@click.argument("task_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def tasks_notes_history(task_id, json_output):
    """Show past versions of a task's notes, newest first."""
    db = DatabaseManager()
    task_id = _resolve_task_id(db, task_id)
    task = db.get_task(task_id)
    if not task:
        console.print(f"[red]Task #{task_id} not found[/red]"); sys.exit(1)
    rows = db.get_task_notes_history(task_id)
    if json_output:
        print(json.dumps(rows, indent=2))
    elif not rows:
        display_id = db.get_task_display_id(task_id) or task_id
        console.print(f"[dim]No notes history for task #{display_id}.[/dim]")
    else:
        display_id = db.get_task_display_id(task_id) or task_id
        console.print(f"Notes history for task #{display_id}:")
        for row in rows:
            console.print(
                f"[{escape(str(row['timestamp']))}] ({escape(str(row['source']))})"
            )
            old = row["old_notes"] if row["old_notes"] is not None else "(empty)"
            new = row["new_notes"] if row["new_notes"] is not None else "(empty)"
            console.print(f"  old: {escape(old)}")
            console.print(f"  new: {escape(new)}")


def _transition_cards(db, task_ids, *, verb, label, noun, step, suffix="", after=None) -> None:
    """Run one per-card step over `task_ids` for the move/done/start/... commands.

    Prints a line per card and keeps going, so one bad id never blocks the
    rest; exits 1 if any requested card was not changed (not found, refused,
    error). A skip is not a failure. `step(db, task_id, task)` does the
    command's own work and returns True (changed), False (failed; it has
    already printed why) or None (skipped). With more than one id it prints
    "<label> N/M <noun><suffix>"; `after(changed)` then runs once, before exit.
    """
    changed = 0
    failed = 0
    for task_id in task_ids:
        try:
            task_id = _resolve_task_id(db, task_id)
            task = db.get_task(task_id)
            if not task:
                print(f"Task #{task_id} not found"); failed += 1; continue
            result = step(db, task_id, task)
            if result:
                changed += 1
            elif result is not None:
                failed += 1
        except Exception as e:
            print(f"Failed to {verb} task #{task_id}: {e}")
            failed += 1
    if len(task_ids) > 1: print(f"\n{label} {changed}/{len(task_ids)} {noun}{suffix}")
    if after: after(changed)
    if failed:
        sys.exit(1)


def _status_step(status, label):
    """Step that sets a card's status and prints "<label>: #id - text"."""
    def step(db, task_id, task):
        db.update_task(task_id, status=status)
        print(f"{label}: #{task_id} - {task['text'][:50]}")
        return True
    return step


def _proposal_step(label, **updates):
    """Step for approve/reject: refuses anything that is not a proposal."""
    def step(db, task_id, task):
        if task.get("task_type") != "proposal":
            print(f"Task #{task_id} is not a proposal (type: {task.get('task_type', 'unknown')})")
            return False
        db.update_task(task_id, **updates)
        print(f"{label}: #{task_id} - {task['text'][:50]}")
        return True
    return step


@tasks_group.command(name="move")
@click.argument("project", type=str)
@click.argument("task_ids", type=int, nargs=-1, required=True)
def tasks_move(project, task_ids):
    """Reassign one or more tasks to a different project."""
    db = DatabaseManager()
    target_project_id = _resolve_project_id(db, project)
    if not target_project_id:
        console.print(f"[red]Project '{project}' not found[/red]"); sys.exit(1)
    target_project = db.get_project(target_project_id)
    target_name = target_project["name"] if target_project else target_project_id

    def step(db, task_id, task):
        old_project = task["project_id"]
        if old_project == target_project_id:
            print(f"Task #{task_id} already in project '{target_name}'"); return None
        db.update_task(task_id, project_id=target_project_id)
        print(f"Moved: #{task_id} from '{old_project}' to '{target_name}'")
        return True

    _transition_cards(db, task_ids, verb="move", label="Moved", noun="tasks",
                      suffix=f" to '{target_name}'", step=step)


@tasks_group.command(name="done")
@click.argument("task_ids", type=int, nargs=-1, required=True)
def tasks_done(task_ids):
    """Mark one or more tasks as Done."""
    db = DatabaseManager()

    def after(changed):
        if changed > 0:
            # Keep the Done column short by HIDING older cards, never deleting them.
            # Per-project so a burst of completions in one project can't evict
            # another project's history (#6870).
            archived = db.archive_done_tasks(keep_per_project=25)
            if archived > 0:
                print(f"Archived {archived} older Done card(s) (keeping 25 most recent per project)")
            print("💡 Tip: Run /compound in Claude Code to journal what you learned")

    mark_done = _status_step("Done", "Done")

    def step(db, task_id, task):
        if not mark_done(db, task_id, task):
            return False
        try:
            # A card can be out of another project's checkout, so match on the card, not the project.
            _print_checkin_reminders(db, [due for due in db.checkin_reminders() if due["task_id"] == task_id])
        except Exception as exc:  # the card IS Done; say the reminder check failed rather than undo that
            console.print(f"[red]Could not check whether a checkout waits on #{task_id}: {escape(str(exc))}[/red]")
        return True

    _transition_cards(db, task_ids, verb="complete", label="Completed", noun="tasks", step=step, after=after)


@tasks_group.command(name="start")
@click.argument("task_ids", type=int, nargs=-1, required=True)
def tasks_start(task_ids):
    """Move one or more tasks to In Progress."""
    db = DatabaseManager()

    def step(db, task_id, task):
        if task.get("task_type") == "agent" and not task.get("prompt"):
            console.print(f"[yellow]Starting agent task #{task_id} without a prompt[/yellow]")
        is_blocked, blocking_ids, reason = _blocked_state(db, task_id)
        if is_blocked:
            if reason:
                console.print(
                    f"[red]Cannot start {_format_task_ref(task_id, db=db)} - blocked: {escape(reason)}[/red]"
                )
                return False
            blocking_str = _format_task_refs(blocking_ids, db=db)
            console.print(
                f"[red]Cannot start {_format_task_ref(task_id, db=db)} - blocked by: {blocking_str}[/red]"
            )
            return False
        return _status_step("In Progress", "Started")(db, task_id, task)

    _transition_cards(db, task_ids, verb="start", label="Started", noun="tasks", step=step)


@tasks_group.command(name="review")
@click.argument("task_ids", type=int, nargs=-1, required=True)
def tasks_review(task_ids):
    """Move one or more tasks to Review."""
    _transition_cards(DatabaseManager(), task_ids, verb="review", label="Reviewed", noun="tasks",
                      step=_status_step("Review", "Review"))


@tasks_group.command(name="cancel")
@click.argument("task_ids", type=int, nargs=-1, required=True)
def tasks_cancel(task_ids):
    """Cancel one or more tasks (soft delete - keeps history)."""
    _transition_cards(DatabaseManager(), task_ids, verb="cancel", label="Cancelled", noun="tasks",
                      step=_status_step("Cancelled", "Cancelled"))


@tasks_group.command(name="approve")
@click.argument("task_ids", type=int, nargs=-1, required=True)
def tasks_approve(task_ids):
    """Approve one or more proposals, converting them to regular backlog tasks."""
    _transition_cards(DatabaseManager(), task_ids, verb="approve", label="Approved", noun="proposals",
                      step=_proposal_step("Approved", task_type="manual"))


@tasks_group.command(name="reject")
@click.argument("task_ids", type=int, nargs=-1, required=True)
def tasks_reject(task_ids):
    """Reject one or more proposals (cancels them)."""
    _transition_cards(DatabaseManager(), task_ids, verb="reject", label="Rejected", noun="proposals",
                      step=_proposal_step("Rejected", status="Cancelled"))


@tasks_group.command(name="delete")
@click.argument("task_id", type=int)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def tasks_delete(task_id, yes):
    """Permanently delete a single task (one at a time for safety)."""
    db = DatabaseManager()
    task_id = _resolve_task_id(db, task_id)
    task = db.get_task(task_id)
    if not task: print(f"Task #{task_id} not found"); sys.exit(1)
    print(f"  #{task['id']} | {task['status']} | {task['text'][:60]}")
    if not yes:
        confirm = click.confirm(f"\nPermanently delete task #{task_id}?", default=False)
        if not confirm: print("Cancelled"); return
    try:
        db.delete_task(task_id)
        print(f"Deleted: #{task_id}")
    except Exception as e:
        print(f"Failed to delete task #{task_id}: {e}")
        sys.exit(1)


@tasks_group.command(name="show")
@click.argument("task_ids", type=int, nargs=-1, required=True)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def tasks_show(task_ids, json_output):
    """Show full details of one or more tasks including prompt."""
    db = DatabaseManager()
    tasks = []
    checkouts = db.get_open_checkouts()

    for i, task_id in enumerate(task_ids):
        try:
            task_id = _resolve_task_id(db, task_id)
            task = db.get_task(task_id)
            if not task:
                if not json_output: console.print(f"[red]Task #{task_id} not found[/red]")
                continue
            display_id = db.get_task_display_id(task_id) or task_id
            if json_output:
                tasks.append({**task, "checkout": checkouts.get(task["project_id"])})
            else:
                if i > 0: print("\n" + "-" * 40 + "\n")
                print(f"Task #{display_id} (pk: {task['id']})")
                print(f"Project: {task['project_id']}")
                checkout = checkouts.get(task["project_id"])
                if checkout: print(f"Project is {_checkout_label(checkout)}")
                print(f"Status: {task['status']}")
                print(f"Priority: {task.get('priority') or 'None'}")
                blocked_report = _blocked_by_report(db, task)
                if blocked_report: print(blocked_report)
                print(f"Created: {task['created_at']}")
                if task.get('created_by'): print(f"Created by: {task['created_by']}")
                print(f"Updated: {task['updated_at']}")
                if task.get('completed_at'): print(f"Completed: {task['completed_at']}")
                print()
                print("Description:")
                print(task['text'])
                if task.get('notes'): print(); print("Notes:"); print(task['notes'])
                if task.get('prompt'): print(); print("Agent Prompt:"); print(task['prompt'])
                if task.get('review_comment'): print(); print("Review Comment:"); print(task['review_comment'])
        except Exception as e:
            if not json_output: console.print(f"[red]Failed to get task #{task_id}: {e}[/red]")
    if json_output:
        import json as json_lib
        if len(tasks) == 1: print(json_lib.dumps(tasks[0], indent=2))
        else: print(json_lib.dumps(tasks, indent=2))


@tasks_group.command(name="next")
@click.option("-p", "--project", default=None, help="Filter by project (auto-detects from cwd)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def tasks_next(project, json_output):
    """Get the next task to work on (highest priority, In Progress first)."""
    db = DatabaseManager()
    if project:
        project_id = _resolve_project_id(db, project)
        if not project_id: print(f"Project '{project}' not found"); sys.exit(1)
    else:
        project_id = _detect_project_from_cwd(db)
    all_tasks = db.get_tasks(project_id=project_id)
    actionable = [t for t in all_tasks if t["status"] in ("In Progress", "To Do")]
    if not actionable: print("No tasks available"); sys.exit(1)
    priority_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, None: 4}
    status_rank = {"In Progress": 0, "To Do": 1}
    actionable.sort(key=lambda t: (status_rank.get(t["status"], 99), priority_rank.get(t.get("priority"), 4)))
    task = actionable[0]
    checkout = db.get_open_checkouts().get(task["project_id"])
    if json_output:
        import json as json_lib
        print(json_lib.dumps({**task, "checkout": checkout}, indent=2))
    else:
        priority = task.get("priority") or "-"
        print(f"#{task['id']} | {task['project_id']} | {task['status']} | {priority} | {task['text']}")
        if checkout: print(f"Project is {_checkout_label(checkout)}: develop it only there.")
    sys.exit(0)


@tasks_group.command(name="tree")
@click.argument("task_id", type=int)
def tasks_tree(task_id):
    """Show dependency tree for a parent task."""
    db = DatabaseManager()
    # Every other tasks subcommand resolves display IDs; this one did not, so
    # `pt tasks tree 6742` reported "not found" for a card that exists (#6747).
    task_id = _resolve_task_id(db, task_id)
    task = db.get_task(task_id)
    if not task: console.print(f"[red]Task #{task_id} not found[/red]"); return
    subtasks = db.get_subtasks(task_id)
    if not subtasks: console.print(f"Task #{task_id} has no subtasks"); return
    progress = db.get_subtask_progress(task_id)
    print(f"\nExecution Tree for #{task_id}: {task['text']}\n")
    print(f"Progress: {progress['done']}/{progress['total']} complete ({progress['percent']}%)\n")
    status_emoji = {"Done": "✅", "In Progress": "●", "To Do": "○", "Backlog": "○"}
    for i, subtask in enumerate(subtasks):
        is_last = (i == len(subtasks) - 1)
        prefix = "└─" if is_last else "├─"
        emoji = status_emoji.get(subtask["status"], "?")
        is_blocked, blocking_ids, reason = _blocked_state(db, subtask["id"])
        block_str = ""
        if is_blocked and reason: block_str = f" 🔒 (blocked: {reason})"
        elif is_blocked: block_str = f" 🔒 (blocked by {_format_task_refs(blocking_ids, db=db)})"
        elif subtask["status"] not in ["Done", "In Progress"]: block_str = " ← READY NOW"
        print(f"{prefix} {emoji} {_format_task_ref(subtask['id'], db=db)} | {subtask['status']:12} | {subtask['text']}{block_str}")
    print("\nUse 'pt tasks next' to see what you can start immediately\n")


@tasks_group.command(name="export")
@click.option("-o", "--output", default="data/tasks_export.json", help="Output file path")
def tasks_export(output):
    """Export all tasks to JSON file for backup."""
    import json as json_lib
    from datetime import datetime, timezone
    db = DatabaseManager()
    # A backup that silently drops archived cards isn't a backup.
    all_tasks = db.get_tasks(include_archived=True)
    export_data = {"exported_at": datetime.now(timezone.utc).isoformat(), "total_count": len(all_tasks), "tasks": all_tasks}
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(output_path, 'w') as f:
            json_lib.dump(export_data, f, indent=2)
        console.print(f"[green]✅ Exported {len(all_tasks)} tasks to {output}[/green]")
    except Exception as e:
        console.print(f"[red]Failed to export tasks: {e}[/red]")


# =============================================================================
# Calendar group
# =============================================================================

def _get_calendar_manager():
    from db.calendar_manager import CalendarManager
    return CalendarManager()


def _display_events(events, json_output=False, cron_jobs=None):
    import json as json_lib
    if json_output:
        out = {"events": events, "total": len(events)}
        if cron_jobs is not None:
            out["cron_jobs"] = cron_jobs
        print(json_lib.dumps(out, indent=2))
        return
    if not events and not cron_jobs:
        print("No events found.")
        return
    if events:
        for ev in events:
            time_str = f" {ev['event_time']}" if ev.get("event_time") else ""
            project_str = f" | {ev['project_id']}" if ev.get("project_id") else ""
            prompt_marker = " [prompt]" if ev.get("prompt") else ""
            print(f"#{ev['id']} | {ev['event_date']}{time_str} | {ev['event_type']} | {ev['status']}{project_str}{prompt_marker} | {ev['title']}")
        print(f"\nEvents: {len(events)}")
    if cron_jobs:
        print(f"\n── Cron Jobs ──")
        for cj in cron_jobs:
            desc_str = f" | {cj['description']}" if cj.get("description") else ""
            print(f"  cron/{cj['id']} | {cj['schedule']} | {cj['project_id']}{desc_str} | {cj['command']}")
        print(f"\nCron jobs: {len(cron_jobs)}")


def _calendar_option_callback(validator, allow_empty=False):
    """Click callback that runs a calendar validator, turning its ValueError into BadParameter.

    With ``allow_empty``, an empty string passes through unvalidated: ``--time ""``
    means "no time" (on update, it clears the stored time).
    """
    def callback(ctx, param, value):
        if value is None or (allow_empty and value == ""):
            return value
        try:
            return validator(value)
        except ValueError as exc:
            raise click.BadParameter(str(exc))
    return callback


_date_option = _calendar_option_callback(validate_event_date)
_time_option = _calendar_option_callback(validate_event_time, allow_empty=True)


@click.group(name="calendar", invoke_without_command=True)
@click.pass_context
@click.option("--days", default=7, type=int, help="Days ahead to show (default: 7)")
@click.option("-p", "--project", default=None, help="Filter by project")
@click.option("-t", "--type", "event_type", default=None, help="Filter by type: reminder, deadline, milestone, meeting")
@click.option("--all", "show_all", is_flag=True, help="Show all events (no date window)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def calendar_group(ctx, days, project, event_type, show_all, json_output):
    """Manage the AI-first calendar.

    Bare `pt calendar` lists upcoming events and cron jobs (--days, -p, -t,
    --all, --json). Subcommands: add, show, update, done, cancel (events);
    crons, add-cron (cron jobs). Dates are YYYY-MM-DD, times are 24-hour HH:MM.
    Deadlines also reach the 7am digest email (scripts/alert_digest.py).

    \b
    Examples:
        pt calendar                        # Upcoming 7 days
        pt calendar --days 30              # Next 30 days
        pt calendar -p my-app              # Filter by project
        pt calendar add "Sprint review" --date 2026-03-28
        pt calendar show 1
        pt calendar done 1
    """
    if ctx.invoked_subcommand is not None:
        return
    db = DatabaseManager()
    project_id = _resolve_project_id(db, project) if project else None
    cm = _get_calendar_manager()
    events = cm.get_events(
        days=days,
        project_id=project_id,
        event_type=event_type,
        include_all=show_all,
    )
    cron_jobs = cm.get_cron_jobs(project_id=project_id)
    _display_events(events, json_output=json_output, cron_jobs=cron_jobs)


@calendar_group.command(name="crons")
@click.option("-p", "--project", default=None, help="Filter by project")
@click.option("--all", "show_all", is_flag=True, help="Include inactive cron jobs")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def calendar_crons(project, show_all, json_output):
    """List all cron jobs.

    \b
    Examples:
        pt calendar crons
        pt calendar crons -p project-tracker --json
    """
    import json as json_lib
    db = DatabaseManager()
    project_id = _resolve_project_id(db, project) if project else None
    cm = _get_calendar_manager()
    cron_jobs = cm.get_cron_jobs(
        project_id=project_id, active_only=not show_all
    )
    if json_output:
        print(json_lib.dumps({"cron_jobs": cron_jobs, "total": len(cron_jobs)}, indent=2))
        return
    if not cron_jobs:
        print("No cron jobs found.")
        return
    print("Cron Jobs\n")
    for cj in cron_jobs:
        desc_str = f" | {cj['description']}" if cj.get("description") else ""
        active_str = "" if cj.get("is_active", 1) else " [inactive]"
        print(f"  cron/{cj['id']} | {cj['schedule']} | {cj['project_id']}{desc_str}{active_str}")
        print(f"    {cj['command']}")
    print(f"\nTotal: {len(cron_jobs)} cron job(s)")


@calendar_group.command(name="add-cron")
@click.argument("project")
@click.argument("schedule")
@click.argument("command")
@click.option("--description", default="", help="What this cron job does")
def calendar_add_cron(project, schedule, command, description):
    """Add a cron job from the calendar.

    \b
    Examples:
        pt calendar add-cron project-tracker "0 7 * * *" "scripts/alert-digest.sh" \\
            --description "Daily digest"
        pt calendar add-cron my-app "0 9 * * 1" "bash scripts/render_replay.sh" \\
            --description "Weekly brain replay render"
    """
    db = DatabaseManager()
    project_id = _resolve_project_id(db, project)
    if not project_id:
        console.print(f"[red]Project '{project}' not found[/red]"); return

    cm = _get_calendar_manager()
    new_id = cm.add_cron_job(project_id, schedule, command, description or None)

    console.print(f"[green]✅ Added cron job #{new_id} to {project}[/green]")
    console.print(f"   Schedule: {schedule}")
    console.print(f"   Command:  {command}")


@calendar_group.command(name="add")
@click.argument("title")
@click.option("--date", "event_date", required=True, callback=_date_option, help="Date: YYYY-MM-DD")
@click.option("--time", "event_time", default=None, callback=_time_option, help="Time: HH:MM, 24-hour (optional)")
@click.option("-t", "--type", "event_type", default="reminder",
              type=click.Choice(["reminder", "deadline", "milestone", "meeting", "recurring"]),
              help="Event type (default: reminder)")
@click.option("-p", "--project", default=None, help="Project name or ID")
@click.option("--prompt", default=None, help="Agent instructions when event fires")
@click.option("--description", default=None, help="Human-readable description")
@click.option("--notify", "notify_before_minutes", default=60, type=int,
              help="Minutes before to notify (default: 60)")
@click.option("--recurrence", default=None,
              type=click.Choice(["daily", "weekly", "monthly"]),
              help="Recurrence pattern")
@click.option("--json", "json_output", is_flag=True, help="Output new event as JSON")
def calendar_add(title, event_date, event_time, event_type, project,
                 prompt, description, notify_before_minutes, recurrence, json_output):
    """Add a calendar event.

    \b
    Examples:
        pt calendar add "Ship v2" --date 2026-03-28 --type milestone
        pt calendar add "Weekly review" --date 2026-03-25 --time 10:00 --recurrence weekly
        pt calendar add "Check cards" --date 2026-03-24 --prompt "Review all In Progress cards"
    """
    import json as json_lib
    db = DatabaseManager()
    project_id = _resolve_project_id(db, project) if project else None
    if project and not project_id:
        console.print(f"[red]Project '{project}' not found[/red]"); return

    cm = _get_calendar_manager()
    try:
        event_id = cm.add_event(
            title=title,
            event_date=event_date,
            event_time=event_time,
            event_type=event_type,
            project_id=project_id,
            prompt=prompt,
            description=description,
            notify_before_minutes=notify_before_minutes,
            recurrence=recurrence,
            created_by="human",
        )
        if json_output:
            print(json_lib.dumps({"id": event_id, "title": title, "event_date": event_date}))
        else:
            console.print(f"[green]✅ Created event #{event_id}: {title} on {event_date}[/green]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")


@calendar_group.command(name="show")
@click.argument("event_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def calendar_show(event_id, json_output):
    """Show full details of a calendar event."""
    import json as json_lib
    cm = _get_calendar_manager()
    event = cm.get_event(event_id)
    if not event:
        console.print(f"[red]Event #{event_id} not found[/red]"); return
    if json_output:
        print(json_lib.dumps(event, indent=2)); return

    console.print(f"\n[bold cyan]#{event['id']} — {event['title']}[/bold cyan]")
    console.print(f"Date:    {event['event_date']}" + (f" {event['event_time']}" if event.get("event_time") else ""))
    console.print(f"Type:    {event['event_type']}")
    console.print(f"Status:  {event['status']}")
    if event.get("project_id"):    console.print(f"Project: {event['project_id']}")
    if event.get("recurrence"):    console.print(f"Recurs:  {event['recurrence']}")
    if event.get("description"):   console.print(f"\n{event['description']}")
    if event.get("prompt"):        console.print(f"\n[dim]Agent prompt:[/dim]\n{event['prompt']}")

    linked = event.get("linked_tasks", [])
    if linked:
        console.print(f"\n[bold]Linked tasks:[/bold]")
        for t in linked:
            console.print(f"  #{t['id']} [{t['status']}] {t['text']} ({t['link_type']})")
    console.print()


@calendar_group.command(name="done")
@click.argument("event_id", type=int)
def calendar_done(event_id):
    """Mark an event as done."""
    cm = _get_calendar_manager()
    if cm.mark_done(event_id):
        console.print(f"[green]✅ Event #{event_id} marked as done[/green]")
    else:
        console.print(f"[red]Event #{event_id} not found[/red]")


@calendar_group.command(name="cancel")
@click.argument("event_id", type=int)
def calendar_cancel(event_id):
    """Cancel an event."""
    cm = _get_calendar_manager()
    if cm.cancel_event(event_id):
        console.print(f"[yellow]Event #{event_id} cancelled[/yellow]")
    else:
        console.print(f"[red]Event #{event_id} not found[/red]")


@calendar_group.command(name="update")
@click.argument("event_id", type=int)
@click.option("--title", default=None)
@click.option("--date", "event_date", default=None, callback=_date_option, help="New date: YYYY-MM-DD")
@click.option("--time", "event_time", default=None, callback=_time_option, help='New time: HH:MM, 24-hour; --time "" clears it')
@click.option("-t", "--type", "event_type", default=None)
@click.option("-p", "--project", default=None)
@click.option("--prompt", default=None)
@click.option("--description", default=None)
@click.option("--notify", "notify_before_minutes", default=None, type=int)
def calendar_update(event_id, title, event_date, event_time, event_type, project,
                    prompt, description, notify_before_minutes):
    """Update any fields on a calendar event."""
    db = DatabaseManager()
    cm = _get_calendar_manager()
    updates = {}
    if title is not None:                 updates["title"] = title
    if event_date is not None:            updates["event_date"] = event_date
    if event_time is not None:            updates["event_time"] = event_time
    if event_type is not None:            updates["event_type"] = event_type
    if prompt is not None:                updates["prompt"] = prompt
    if description is not None:           updates["description"] = description
    if notify_before_minutes is not None: updates["notify_before_minutes"] = notify_before_minutes
    if project is not None:
        pid = _resolve_project_id(db, project)
        if not pid:
            console.print(f"[red]Project '{project}' not found[/red]"); return
        updates["project_id"] = pid

    if not updates:
        console.print("[yellow]No updates provided[/yellow]"); return
    try:
        if cm.update_event(event_id, **updates):
            console.print(f"[green]Updated event #{event_id}[/green]")
        else:
            console.print(f"[red]Event #{event_id} not found[/red]")
    except ValueError as e:
        console.print(f"[red]{e}[/red]")


# =============================================================================
# Memory group — wraps an optional brain.py memory store (scripts/config.py [memory])
# =============================================================================


class PtJsonError(Exception):
    """Structured CLI error for JSON automation surfaces."""

    def __init__(self, error_class: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.exit_code = exit_code


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _emit_json(payload: dict) -> None:
    click.echo(_json_dumps(payload))


def _emit_json_error(error: PtJsonError, command: str, *, err: bool = False) -> NoReturn:
    """Emit a structured JSON error and exit. Never returns.

    `err=True` routes the JSON to stderr instead of stdout — required for
    NDJSON streams (e.g. `memory export`) so a mid-stream error doesn't
    pollute the data channel.
    """
    payload = {
        "schema_version": PT_JSON_SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "error": {
            "class": error.error_class,
            "message": error.message,
        },
    }
    click.echo(_json_dumps(payload), err=err)
    raise SystemExit(error.exit_code)


def _parse_since_until(value: str | None, option_name: str) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    import re
    from datetime import timedelta, timezone

    match = re.fullmatch(r"(\d+)([dhw])", raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "d": timedelta(days=amount),
            "h": timedelta(hours=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%d %H:%M:%S")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PtJsonError(
            "validation",
            f"{option_name} must be ISO datetime/date or relative duration like 7d, 12h, 2w",
            EXIT_VALIDATION,
        ) from exc
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _memory_db_path() -> Path | None:
    """The memory store's database: $PT_MEMORY_DB_PATH, else the configured one, else None.

    A local config that can't be read raises PtJsonError, so every command
    reports it in its own error format. Call once per command and pass the
    result on: the file can change while a command runs.
    """
    override = os.environ.get("PT_MEMORY_DB_PATH")
    if override:
        return Path(override).expanduser()
    try:
        integration = memory_integration()
    except ValueError as exc:
        raise PtJsonError("validation", str(exc), EXIT_VALIDATION) from exc
    return integration.brain_db if integration else None


def _require_memory_db_path() -> Path:
    """_memory_db_path(), or backend_unavailable when the memory integration is off."""
    db_path = _memory_db_path()
    if db_path is None:
        raise PtJsonError(
            "backend_unavailable",
            MEMORY_NOT_CONFIGURED.format(path=local_config_path()),
            EXIT_BACKEND_UNAVAILABLE,
        )
    return db_path


def _open_memory_db_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise PtJsonError(
            "backend_unavailable",
            f"memory database not found at {db_path}",
            EXIT_BACKEND_UNAVAILABLE,
        )
    # The memory store owns this source; pt only reads it.
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as exc:
        raise PtJsonError(
            "backend_unavailable",
            f"could not open memory database read-only: {exc}",
            EXIT_BACKEND_UNAVAILABLE,
        ) from exc


def _metadata_dict(row: sqlite3.Row) -> dict:
    raw = row["metadata"]
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:  # governance: allow-silent SF002: a malformed metadata blob in the memory store's read-only rows is treated like a non-dict one; the row and its content are still returned with blank derived fields
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _memory_row_to_dict(row: sqlite3.Row) -> dict:
    metadata = _metadata_dict(row)
    return {
        "id": row["id"],
        "uuid": row["uuid"],
        "content": row["content"],
        "created_at": row["created_at"],
        "source_machine": row["source_machine"] or "",
        "scope": row["scope"] or "",
        "agent_family": row["agent_family"] or metadata.get("agent_family", ""),
        "source_agent": metadata.get("source_agent", ""),
        "project": metadata.get("project", ""),
        "type": metadata.get("type", "observation"),
        "content_type": metadata.get("content_type", ""),
        "metadata": metadata,
    }


def _memory_where_clauses(
    since: str | None,
    until: str | None,
    source: str | None,
    project: str | None,
    query: str | None = None,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if query:
        clauses.append("(content LIKE ? OR metadata LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at <= ?")
        params.append(until)
    if source:
        clauses.append("(source_machine = ? OR json_extract(metadata, '$.source_agent') = ?)")
        params.extend([source, source])
    if project:
        clauses.append("json_extract(metadata, '$.project') = ?")
        params.append(project)
    return clauses, params


def _memory_query_rows(
    *,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int, str]:
    if limit < 1 or limit > 500:
        raise PtJsonError("validation", "--limit must be between 1 and 500", EXIT_VALIDATION)
    if offset < 0:
        raise PtJsonError("validation", "--offset must be >= 0", EXIT_VALIDATION)
    since_sql = _parse_since_until(since, "--since")
    until_sql = _parse_since_until(until, "--until")
    clauses, params = _memory_where_clauses(since_sql, until_sql, source, project, query)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT id, uuid, content, metadata, scope, agent_family, created_at, source_machine "
        f"FROM thoughts {where_sql} "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    count_sql = f"SELECT COUNT(*) FROM thoughts {where_sql}"
    db_path = _require_memory_db_path()
    try:
        with _open_memory_db_readonly(db_path) as conn:
            total = int(conn.execute(count_sql, params).fetchone()[0])
            rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    except PtJsonError:
        raise
    except sqlite3.Error as exc:
        raise PtJsonError("query_failure", f"memory query failed: {exc}", EXIT_QUERY_FAILURE) from exc
    return [_memory_row_to_dict(row) for row in rows], total, db_path.as_posix()


def _memory_payload(command: str, rows: list[dict], total: int, db_path: str,
                    limit: int, offset: int, filters: dict) -> dict:
    return {
        "schema_version": PT_MEMORY_SCHEMA_VERSION,
        "ok": True,
        "command": command,
        "read_only": True,
        "backend": {
            "type": "sqlite",
            "path": db_path,
        },
        "filters": filters,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
            "total": total,
        },
        "results": rows,
    }


def _run_brain(*args: str) -> None:
    """Call the configured memory store's brain.py with uv run.

    With doppler_project/doppler_config set it runs under ``doppler run`` so
    brain.py gets its own secrets; ``pt`` itself runs without Doppler.
    """
    try:
        integration = memory_integration()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if integration is None:
        raise click.ClickException(MEMORY_NOT_CONFIGURED.format(path=local_config_path()))
    if not integration.brain_py.exists():
        raise click.ClickException(f"brain.py not found at {integration.brain_py}")
    cmd = ["uv", "run", str(integration.brain_py), *args]
    if integration.doppler_project:
        if not shutil.which("doppler"):
            raise click.ClickException("doppler CLI not found. Install with: brew install doppler")
        cmd = ["doppler", "run", "--project", integration.doppler_project,
               "--config", integration.doppler_config, "--", *cmd]
    # No timeout on purpose: `graph build` legitimately runs for most of an hour.
    result = subprocess.run(cmd, check=False, cwd=str(integration.root))
    if result.returncode != 0:
        # brain.py already printed its own error; pass its failure on.
        sys.exit(result.returncode)


@click.group(name="memory", invoke_without_command=True)
@click.pass_context
def memory_group(ctx: click.Context) -> None:
    """Search and write to the shared agent memory store.

    A thin wrapper around the configured store's brain.py so every agent can access
    cross-agent memory without knowing the full path or needing MCP approval.

    \b
    Quick start:
      pt memory search "what did we decide about backups?"
      pt memory write "Decision: use Haiku for routine tasks" --type decision
      pt memory stats

    On success the read-only --json modes (search, recent, stats) print one
    JSON object carrying schema_version, ok and read_only beside its data;
    export prints NDJSON. When those modes or export fail their own checks they exit 2
    (invalid input), 3 (memory backend unavailable) or 4 (query failed).
    Option parsing errors, such as a non-integer --limit, are Click's plain
    usage message with exit 2.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@memory_group.command(name="search")
@click.argument("query_arg", required=False)
@click.option("--query", "query_opt", default=None, help="Search text for JSON/non-interactive mode")
@click.option("--top", "-n", default=5, type=int, help="Number of results (default: 5)")
@click.option("--agent-family", "-a", default="",
              help="Filter to agent family: claude | gemini | codex | antigravity | qwen")
@click.option("--namespace", default="", help="Filter by project prefix (e.g. loop/sales)")
@click.option("--goal-id", default="", help="Filter to loop_state entries for a specific goal ID")
@click.option("--since", default=None, help="Only include rows at/after ISO time or duration like 7d")
@click.option("--until", default=None, help="Only include rows at/before ISO time or duration like 7d")
@click.option("--source", default=None, help="Filter by source_machine or metadata.source_agent")
@click.option("--project", default=None, help="Filter by metadata.project")
@click.option("--limit", default=None, type=int, help="Maximum rows for JSON/non-interactive mode")
@click.option("--offset", default=0, type=int, help="Pagination offset for JSON/non-interactive mode")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def memory_search(query_arg: str | None, query_opt: str | None, top: int, agent_family: str,
                  namespace: str, goal_id: str, since: str | None, until: str | None,
                  source: str | None, project: str | None, limit: int | None,
                  offset: int, json_output: bool) -> None:
    """Search the shared brain by semantic similarity.

    \b
    Examples:
      pt memory search "what was the backup decision?"
      pt memory search "MCP firewall" --top 10
      pt memory search "calendar" --agent-family claude
    """
    query = query_opt or query_arg
    if not query:
        if json_output:
            _emit_json_error(
                PtJsonError("validation", "memory search requires QUERY or --query", EXIT_VALIDATION),
                "memory.search",
            )
        raise click.UsageError("memory search requires QUERY or --query")
    effective_limit = limit if limit is not None else top
    direct_mode = json_output or any([since, until, source, project, limit is not None, offset])
    if direct_mode:
        try:
            rows, total, db_path = _memory_query_rows(
                query=query,
                since=since,
                until=until,
                source=source,
                project=project,
                limit=effective_limit,
                offset=offset,
            )
        except PtJsonError as exc:
            if json_output:
                _emit_json_error(exc, "memory.search")
            raise click.ClickException(exc.message) from exc
        payload = _memory_payload(
            "memory.search",
            rows,
            total,
            db_path,
            effective_limit,
            offset,
            {
                "query": query,
                "since": since,
                "until": until,
                "source": source,
                "project": project,
            },
        )
        if json_output:
            _emit_json(payload)
            return
        for row in rows:
            click.echo(f"#{row['id']} | {row['created_at']} | {row['project']} | {row['type']} | {row['content'][:160]}")
        click.echo(f"\nReturned {len(rows)} of {total}")
        return

    args = ["search", query, "--top", str(top)]
    if agent_family:
        args += ["--agent-family", agent_family]
    if namespace:
        args += ["--namespace", namespace]
    if goal_id:
        args += ["--goal-id", goal_id]
    _run_brain(*args)


@memory_group.command(name="write")
@click.argument("content")
@click.option("--type", "-t", "entry_type", default="observation",
              help="Type: observation | decision | question | insight | error (default: observation)")
@click.option("--project", "-p", default="", help="Project name to tag this entry")
@click.option("--agent-family", "-a", default="",
              help="Agent family: claude | gemini | codex | antigravity | qwen")
@click.option("--scope", "-s", default="",
              help="Scope: shared | agent-scoped (auto-classified if omitted)")
def memory_write(content: str, entry_type: str, project: str, agent_family: str, scope: str) -> None:
    """Write a new entry to the shared brain.

    \b
    Examples:
      pt memory write "Decision: use Haiku for all routine tasks" --type decision
      pt memory write "Bug: pt scan hangs without Discord timeout" --type error --project project-tracker
      pt memory write "Trust layer is the right thesis" --type insight --project data-vault-factory
    """
    # Auto-detect project from cwd when not explicitly provided
    if not project:
        import os
        cwd = os.environ.get("PT_CALLER_CWD", os.getcwd())
        project = os.path.basename(cwd)
    args = ["write", content, "--type", entry_type]
    args += ["--project", project]
    if agent_family:
        args += ["--agent-family", agent_family]
    if scope:
        args += ["--scope", scope]
    _run_brain(*args)


@memory_group.command(name="recent")
@click.option("--since", default=None, help="Only include rows at/after ISO time or duration like 7d")
@click.option("--until", default=None, help="Only include rows at/before ISO time or duration like 7d")
@click.option("--source", default=None, help="Filter by source_machine or metadata.source_agent")
@click.option("--project", default=None, help="Filter by metadata.project")
@click.option("--limit", default=50, type=int, help="Maximum rows")
@click.option("--offset", default=0, type=int, help="Pagination offset")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def memory_recent(since, until, source, project, limit, offset, json_output) -> None:
    """List recent memory entries through a read-only DB connection."""
    try:
        rows, total, db_path = _memory_query_rows(
            since=since,
            until=until,
            source=source,
            project=project,
            limit=limit,
            offset=offset,
        )
    except PtJsonError as exc:
        if json_output:
            _emit_json_error(exc, "memory.recent")
        raise click.ClickException(exc.message) from exc
    payload = _memory_payload(
        "memory.recent",
        rows,
        total,
        db_path,
        limit,
        offset,
        {"since": since, "until": until, "source": source, "project": project},
    )
    if json_output:
        _emit_json(payload)
        return
    for row in rows:
        click.echo(f"#{row['id']} | {row['created_at']} | {row['project']} | {row['type']} | {row['content'][:160]}")
    click.echo(f"\nReturned {len(rows)} of {total}")


@memory_group.command(name="stats")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def memory_stats(json_output) -> None:
    """Show brain database statistics and per-agent search efficiency."""
    if json_output:
        try:
            db_path = _require_memory_db_path()
            with _open_memory_db_readonly(db_path) as conn:
                total = int(conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0])
                by_project = [
                    {"project": row[0] or "", "count": row[1]}
                    for row in conn.execute(
                        "SELECT json_extract(metadata, '$.project') AS project, COUNT(*) "
                        "FROM thoughts GROUP BY project ORDER BY COUNT(*) DESC LIMIT 50"
                    ).fetchall()
                ]
                by_source = [
                    {"source": row[0] or "", "count": row[1]}
                    for row in conn.execute(
                        "SELECT COALESCE(json_extract(metadata, '$.source_agent'), source_machine, '') AS source, COUNT(*) "
                        "FROM thoughts GROUP BY source ORDER BY COUNT(*) DESC LIMIT 50"
                    ).fetchall()
                ]
                bounds = conn.execute("SELECT MIN(created_at), MAX(created_at) FROM thoughts").fetchone()
        except PtJsonError as exc:
            _emit_json_error(exc, "memory.stats")
        except sqlite3.Error as exc:
            _emit_json_error(
                PtJsonError("query_failure", f"memory stats failed: {exc}", EXIT_QUERY_FAILURE),
                "memory.stats",
            )
        _emit_json({
            "schema_version": PT_MEMORY_SCHEMA_VERSION,
            "ok": True,
            "command": "memory.stats",
            "read_only": True,
            "backend": {"type": "sqlite", "path": db_path.as_posix()},
            "stats": {
                "total": total,
                "oldest_created_at": bounds[0],
                "newest_created_at": bounds[1],
                "by_project": by_project,
                "by_source": by_source,
            },
        })
        return
    _run_brain("stats")


@memory_group.command(name="export")
@click.option("--format", "export_format", type=click.Choice(["ndjson"]), default="ndjson")
@click.option("--since", default=None, help="Only include rows at/after ISO time or duration like 7d")
@click.option("--until", default=None, help="Only include rows at/before ISO time or duration like 7d")
@click.option("--source", default=None, help="Filter by source_machine or metadata.source_agent")
@click.option("--project", default=None, help="Filter by metadata.project")
@click.option("--limit", default=500, type=int, help="Maximum rows")
@click.option("--offset", default=0, type=int, help="Pagination offset")
def memory_export(export_format, since, until, source, project, limit, offset) -> None:
    """Export memory rows as newline-delimited JSON for downstream analysis."""
    try:
        rows, _, _ = _memory_query_rows(
            since=since,
            until=until,
            source=source,
            project=project,
            limit=limit,
            offset=offset,
        )
    except PtJsonError as exc:
        _emit_json_error(exc, "memory.export", err=True)
    for row in rows:
        click.echo(json.dumps(row, sort_keys=True))


# =============================================================================
# Memory graph (wraps brain.py graph subcommands)
# =============================================================================


@click.group(name="graph", invoke_without_command=True)
@click.pass_context
def graph_group(ctx: click.Context) -> None:
    """Query and manage the memory store's knowledge graph.

    The store extracts structured nodes (projects, people, artifacts,
    concepts, tasks, decisions) and edges (co_mentioned, belongs_to, relates_to)
    from all memories in brain.db.

    \b
    Node types:  project, person, artifact, concept, task, decision, tag
    Edge types:  co_mentioned, belongs_to, involved_in, tracked_in, relates_to

    \b
    Quick start:
      pt graph stats                          # Node/edge/confidence counts
      pt graph query project-tracker          # What's connected to a project
      pt graph find "backup strategy"         # Semantic search over nodes
      pt graph path project-tracker Doppler  # Shortest path between nodes
      pt graph build                          # Rebuild from all memories
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@graph_group.command(name="stats")
def graph_stats() -> None:
    """Show memory graph statistics — node/edge counts by type.

    \b
    Example output:
      Nodes: 1,498 (971 artifacts, 228 tasks, 193 concepts, ...)
      Edges: 11,225 (co_mentioned, belongs_to, relates_to, ...)
    """
    _run_brain("graph", "stats")


@graph_group.command(name="query")
@click.argument("project", required=False, default="")
@click.option("--hops", "-h", default=1, type=int,
              help="Degrees of separation from project node (default: 1)")
@click.option("--limit", "-n", default=20, type=int,
              help="Max nodes to return (default: 20)")
@click.option("--types", "-t", default="",
              help="Comma-separated node types to filter (e.g. decision,person)")
@click.option("--since", "-s", default="",
              help="Only nodes updated on/after this date (e.g. 2026-04-04)")
@click.option("--json", "as_json", is_flag=True,
              help="Output as JSON for agent consumption")
def graph_query(project: str, hops: int, limit: int, types: str, since: str, as_json: bool) -> None:
    """Query nodes connected to a project.

    Returns people, files, decisions, concepts, and tasks linked to the
    given project within N hops. Project auto-detected from cwd if omitted.
    Nodes ranked by freshness (mention count * temporal decay, 14-day half-life).

    \b
    Examples:
      pt graph query                              # Auto-detect from cwd
      pt graph query project-tracker              # Direct connections
      pt graph query project-tracker --hops 2         # Two degrees out
      pt graph query my-app --types decision,person  # Filter by type
      pt graph query my-app --since 2026-04-04       # What's new?
      pt graph query my-app --json                # JSON for agents
    """
    if not project:
        project = os.path.basename(os.getcwd())
    args = ["graph", "query", project, "--hops", str(hops), "--limit", str(limit)]
    if types:
        args.extend(["--types", types])
    if since:
        args.extend(["--since", since])
    if as_json:
        args.append("--json")
    _run_brain(*args)


@graph_group.command(name="find")
@click.argument("query")
@click.option("--top", "-k", default=10, type=int,
              help="Number of top matches (default: 10)")
@click.option("--hops", "-h", default=1, type=int,
              help="Expand to N-hop neighbors (0 = exact matches only)")
@click.option("--json", "as_json", is_flag=True,
              help="Output as JSON for agent consumption")
def graph_find(query: str, top: int, hops: int, as_json: bool) -> None:
    """Semantic search over graph nodes.

    Finds the graph nodes most similar to your query using embeddings,
    then expands to their neighbors. The 'what does the graph know about X?' query.

    \b
    Examples:
      pt graph find "authentication"              # What's related to auth?
      pt graph find "backup strategy" --top 5     # Focused search
      pt graph find "LoRA training" --hops 0      # Exact matches only
      pt graph find "deployment" --json            # JSON for agents
    """
    args = ["graph", "find", query, "--top", str(top), "--hops", str(hops)]
    if as_json:
        args.append("--json")
    _run_brain(*args)


@graph_group.command(name="path")
@click.argument("source")
@click.argument("target")
@click.option("--max-depth", "-d", default=5, type=int,
              help="Max BFS depth (default: 5)")
@click.option("--json", "as_json", is_flag=True,
              help="Output as JSON for agent consumption")
def graph_path(source: str, target: str, max_depth: int, as_json: bool) -> None:
    """Find shortest path between two nodes.

    BFS traversal from source to target. Shows the chain of nodes and
    edge types connecting them. Useful for understanding how projects relate.

    \b
    Examples:
      pt graph path my-app project-tracker        # How are they connected?
      pt graph path my-app other-app             # Cross-project links
      pt graph path my-app Doppler --json         # JSON for agents
    """
    args = ["graph", "path", source, target, "--max-depth", str(max_depth)]
    if as_json:
        args.append("--json")
    _run_brain(*args)


@graph_group.command(name="export")
def graph_export() -> None:
    """Export the full memory graph as JSON (nodes + edges).

    Outputs to stdout. Pipe to a file or tool:

    \b
    Examples:
      pt graph export                    # Print to terminal
      pt graph export > graph.json       # Save to file
      pt graph export | jq '.nodes | length'  # Count nodes
    """
    _run_brain("graph", "export")


@graph_group.command(name="build")
@click.option("--force", is_flag=True, help="Truncate existing graph before rebuild")
def graph_build(force: bool) -> None:
    """Rebuild the memory graph from all memories.

    Scans every entry in brain.db, extracts entities (projects, people,
    files, concepts, tasks, decisions), builds edges from co-mentions
    and semantic similarity, and writes to graph_nodes/graph_edges tables.

    \b
    Examples:
      pt graph build           # Incremental update
      pt graph build --force   # Full rebuild from scratch
    """
    args = ["graph", "build"]
    if force:
        args.append("--force")
    _run_brain(*args)


# =============================================================================
# Worktrees management
# =============================================================================

@click.group(name="worktrees", invoke_without_command=True)
@click.pass_context
def worktrees_group(ctx):
    """Manage .claude/worktrees/ created by sub-agents."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(worktrees_list)


@worktrees_group.command(name="list")
def worktrees_list():
    """List all worktrees in .claude/worktrees/."""
    repo_root = _find_repo_root()
    if repo_root is None:
        console.print("[red]Not inside a git repository[/red]")
        return
    wt_dir = repo_root / ".claude" / "worktrees"
    if not wt_dir.exists():
        console.print("[dim]No .claude/worktrees/ directory found[/dim]")
        return
    entries = sorted(p for p in wt_dir.iterdir() if p.is_dir())
    if not entries:
        console.print("[dim]No worktrees found[/dim]")
        return
    # Get merged branches
    merged = _get_merged_branches(repo_root)
    console.print(f"\n[bold]Worktrees ({len(entries)})[/bold]\n")
    for entry in entries:
        branch = _worktree_branch(repo_root, entry)
        status = "[green]merged[/green]" if branch and branch in merged else "[yellow]active[/yellow]"
        branch_label = branch or "[dim]detached[/dim]"
        console.print(f"  {entry.name}  {branch_label}  {status}")
    console.print()


@worktrees_group.command(name="clean")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
@click.option("--force", is_flag=True, help="Also remove worktrees with uncommitted changes")
def worktrees_clean(dry_run, force):
    """Remove worktrees whose branches are merged to main or orphaned."""
    repo_root = _find_repo_root()
    if repo_root is None:
        console.print("[red]Not inside a git repository[/red]")
        return
    wt_dir = repo_root / ".claude" / "worktrees"
    if not wt_dir.exists():
        console.print("[dim]No .claude/worktrees/ directory found[/dim]")
        return
    entries = sorted(p for p in wt_dir.iterdir() if p.is_dir())
    if not entries:
        console.print("[dim]No worktrees to clean[/dim]")
        return

    merged = _get_merged_branches(repo_root)
    removed = []
    kept = []

    for entry in entries:
        branch = _worktree_branch(repo_root, entry)
        is_merged = branch and branch in merged
        is_orphaned = branch is None  # no branch ref found

        if is_merged or is_orphaned:
            reason = "merged" if is_merged else "orphaned"
            if dry_run:
                console.print(f"  [yellow]would remove[/yellow] {entry.name} ({reason}, branch: {branch or 'none'})")
                removed.append(entry.name)
            else:
                ok = _remove_worktree(repo_root, entry, force=force)
                if ok:
                    console.print(f"  [green]removed[/green] {entry.name} ({reason})")
                    removed.append(entry.name)
                    # Clean up the branch if it was a worktree-specific branch
                    if branch and branch.startswith("worktree-"):
                        _delete_local_branch(repo_root, branch)
                else:
                    console.print(f"  [red]failed to remove[/red] {entry.name} (use --force?)")
                    kept.append(entry.name)
        else:
            console.print(f"  [cyan]keeping[/cyan] {entry.name} (branch: {branch}, not merged)")
            kept.append(entry.name)

    prefix = "[bold yellow]Dry run:[/bold yellow] " if dry_run else ""
    console.print(f"\n{prefix}Removed {len(removed)}, kept {len(kept)}")


def _find_repo_root() -> Optional[Path]:
    """The git repo root of the directory the operator ran `pt` from.

    This used to try project-tracker's own root FIRST — `Path(__file__).parent
    .parent` — which always exists, so it returned project-tracker
    unconditionally no matter where `pt` was invoked. Its callers are
    `pt worktrees list` and `pt worktrees clean`, and clean REMOVES worktrees,
    so running it from another repo operated on project-tracker's worktrees
    instead. Same wrong-target family as #6851 (#6883).

    `Path.cwd()` is no help either: the launcher does `cd "$launcher_dir"`
    before exec'ing, so cwd inside pt.py is always project-tracker. The
    caller's real directory arrives as PT_CALLER_CWD.
    """
    root = _caller_repo_root()
    if (root / ".git").exists():
        return root
    # _caller_repo_root falls back to the caller cwd when it is not a repo;
    # walk up from there before giving up.
    cur = root
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return None


def _get_merged_branches(repo_root: Path) -> set:
    """Return set of branch names that are merged into main."""
    try:
        result = subprocess.run(
            ["git", "branch", "--merged", "main"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
            check=True,
        )
        branches = set()
        for line in result.stdout.splitlines():
            name = line.strip().lstrip("* ").lstrip("+ ")
            if name and name != "main":
                branches.add(name)
        return branches
    except subprocess.CalledProcessError as e:  # governance: allow-silent SF002: CLI prints the warning; an empty set makes every worktree read as unmerged, so `pt worktrees clean` keeps them (fails safe)
        console.print(f"[yellow]Warning: Failed to check merged branches: {e.stderr.strip()}[/yellow]")
        return set()
    except subprocess.TimeoutExpired:  # governance: allow-silent SF002: CLI prints the warning; an empty set makes every worktree read as unmerged, so `pt worktrees clean` keeps them (fails safe)
        console.print("[yellow]Warning: Timed out checking merged branches[/yellow]")
        return set()


def _worktree_branch(repo_root: Path, wt_path: Path) -> Optional[str]:
    """Get the branch name for a worktree, or None if detached/missing.

    A git failure raises ClickException: `pt worktrees clean` treats None as
    "orphaned" and removes the worktree, so a failed lookup must never read
    as None.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
            check=True,
        )
        # Parse porcelain output: blocks separated by blank lines
        current_path = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current_path = line[len("worktree "):]
            elif line.startswith("branch ") and current_path:
                if Path(current_path).resolve() == wt_path.resolve():
                    # branch refs/heads/foo -> foo
                    ref = line[len("branch "):]
                    return ref.replace("refs/heads/", "")
        return None
    except subprocess.CalledProcessError as e:
        raise click.ClickException(
            f"Failed to determine branch for {wt_path.name}: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise click.ClickException(
            f"Timed out determining branch for {wt_path.name}"
        ) from e


def _remove_worktree(repo_root: Path, wt_path: Path, force: bool = False) -> bool:
    """Remove a worktree using git worktree remove."""
    cmd = ["git", "worktree", "remove", str(wt_path)]
    if force:
        cmd.append("--force")
    try:
        result = subprocess.run(
            cmd, cwd=str(repo_root),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            console.print(f"[yellow]Warning: git worktree remove failed for {wt_path.name}: {result.stderr.strip()}[/yellow]")
            return False
        return True
    except subprocess.TimeoutExpired:  # governance: allow-silent SF002: False is the "not removed" result; worktrees_clean prints "failed to remove" and keeps the entry
        console.print(f"[yellow]Warning: Timed out removing worktree {wt_path.name}[/yellow]")
        return False


def _delete_local_branch(repo_root: Path, branch: str) -> None:
    """Delete a local branch (merged only, safe -d flag)."""
    try:
        result = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            console.print(f"[yellow]Warning: Failed to delete branch {branch}: {result.stderr.strip()}[/yellow]")
    except subprocess.TimeoutExpired:
        console.print(f"[yellow]Warning: Timed out deleting branch {branch}[/yellow]")


# =============================================================================
# Project Info — centralized key-value reference store
# =============================================================================

@click.group(name="info", invoke_without_command=True)
@click.option("-p", "--project", default=None, help="Show info for a specific project")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.pass_context
def info_group(ctx, project, json_output):
    """Centralized project and infrastructure reference.

    \b
    Quick start:
      pt info                              # Show global info
      pt info -p project-tracker           # Show project-specific info
      pt info set domain example.com       # Set global field
      pt info set tech_stack Python -p X   # Set project field
      pt info get domain                   # Get single global field
      pt info delete domain                # Delete global field
      pt info list                         # Show all entries

    \b
    Global keys (pt info):
      semantic_search      grep/rg setup (grepai config, ollama model)
      projects_root        Path to ~/projects
      db_backend           Database backend (local SQLite)
      default_doppler_config  Default doppler config (dev)
      mac_mini_ssh         SSH connection string for Mac Mini
      git_identity         gha vs git vs gh conventions (personal identity)
      pr_merge_policy      PR review/merge procedure

    \b
    Per-project keys (pt info -p <project>):
      tech_stack           Languages, frameworks, key dependencies
      deploy_target        Where it runs (Vercel, Railway, Docker, local)
      doppler              Doppler project/config (e.g. project-tracker/dev)
      infrastructure       Databases, services, cron jobs, external deps
    """
    if ctx.invoked_subcommand is not None:
        return
    db = DatabaseManager()
    if project:
        entries = db.get_info(project_id=project)
        label = f"{project} Info"
    else:
        entries = db.get_info(project_id=None)
        label = "Global Info"
    if json_output:
        import json as json_mod
        click.echo(json_mod.dumps(entries, indent=2))
        return
    if not entries:
        click.echo(f"{label}: (empty)")
        return
    click.echo(f"{label}:")
    for e in entries:
        click.echo(f"  {e['key']}: {e['value']}")


@info_group.command(name="set")
@click.argument("key")
@click.argument("value")
@click.option("-p", "--project", default=None, help="Project to scope this entry to")
def info_set(key, value, project):
    """Set a key-value info entry.

    \b
    Examples:
      pt info set domain example.com
      pt info set tech_stack "Python, Flask, SQLite" -p project-tracker
    """
    db = DatabaseManager()
    db.set_info(key, value, project_id=project)
    scope = f" (project: {project})" if project else " (global)"
    click.echo(f"Set {key} = {value}{scope}")


@info_group.command(name="get")
@click.argument("key")
@click.option("-p", "--project", default=None, help="Project scope")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def info_get(key, project, json_output):
    """Get a single info entry by key.

    \b
    Examples:
      pt info get domain
      pt info get tech_stack -p project-tracker
    """
    import json as json_mod
    db = DatabaseManager()
    entries = db.get_info(project_id=project, key=key)
    if not entries:
        scope = f" for project '{project}'" if project else " (global)"
        click.echo(f"No entry found for '{key}'{scope}")
        return
    if json_output:
        click.echo(json_mod.dumps(entries[0], indent=2))
    else:
        click.echo(entries[0]["value"])


@info_group.command(name="delete")
@click.argument("key")
@click.option("-p", "--project", default=None, help="Project scope")
def info_delete(key, project):
    """Delete an info entry.

    \b
    Examples:
      pt info delete domain
      pt info delete tech_stack -p project-tracker
    """
    db = DatabaseManager()
    deleted = db.delete_info(key, project_id=project)
    if deleted:
        scope = f" from project '{project}'" if project else " (global)"
        click.echo(f"Deleted '{key}'{scope}")
    else:
        click.echo(f"No entry found for '{key}'")


@info_group.command(name="list")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def info_list(json_output):
    """List all info entries across all projects.

    \b
    Examples:
      pt info list
      pt info list --json
    """
    import json as json_mod
    db = DatabaseManager()
    entries = db.get_info(project_id="__all__")
    if json_output:
        click.echo(json_mod.dumps(entries, indent=2))
        return
    if not entries:
        click.echo("No info entries found.")
        return
    # Group by project
    global_entries = [e for e in entries if e["project_id"] is None]
    project_entries = {}
    for e in entries:
        if e["project_id"] is not None:
            project_entries.setdefault(e["project_id"], []).append(e)
    if global_entries:
        click.echo("Global:")
        for e in global_entries:
            click.echo(f"  {e['key']}: {e['value']}")
    for pid in sorted(project_entries.keys()):
        click.echo(f"\n{pid}:")
        for e in project_entries[pid]:
            click.echo(f"  {e['key']}: {e['value']}")


# =============================================================================
# Config / Doctor — non-interactive automation diagnostics
# =============================================================================

def _effective_config_payload() -> dict:
    # One read of the config gives its health, checked for every section, and
    # the memory path, so the two can't disagree if the file changes meanwhile.
    # Its health is reported even when $PT_MEMORY_DB_PATH overrides the path:
    # other integrations read the same file.
    config_error = integration = None
    try:
        data = local_config()
        integration = memory_integration(data)
        chat_addresses(data)
        digest_integration(data)
        morning_integration(data)
        tailor_rules(data)
        fit_keywords(data)
        graph_settings(data)
    except ValueError as exc:
        config_error = str(exc)
    override = os.environ.get("PT_MEMORY_DB_PATH")
    if override:
        memory_db = Path(override).expanduser()
    else:
        memory_db = integration.brain_db if integration else None
    return {
        "schema_version": PT_CONFIG_SCHEMA_VERSION,
        "ok": config_error is None,
        "local_config_error": config_error,
        "project_root": str(Path(__file__).resolve().parent.parent),
        "projects_root": str(PROJECTS_BASE_DIR),
        "tracker_db_path": str(get_db_path()),
        "memory_db_path": str(memory_db) if memory_db else None,
        "local_config_path": str(local_config_path()),
        "backend": "local",
        "env": {
            "PT_DB_PATH": os.environ.get("PT_DB_PATH"),
            "PT_MEMORY_DB_PATH": os.environ.get("PT_MEMORY_DB_PATH"),
            "PROJECTS_ROOT": os.environ.get("PROJECTS_ROOT"),
            "PT_NO_BANNER": os.environ.get("PT_NO_BANNER"),
        },
    }


@click.group(name="config", invoke_without_command=True)
@click.pass_context
def config_group(ctx: click.Context) -> None:
    """Show effective pt runtime configuration."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@config_group.command(name="show")
@click.option("--effective", is_flag=True, help="Show resolved runtime configuration")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def config_show(effective: bool, json_output: bool) -> None:
    """Show effective configuration for cron/SSH debugging."""
    if not effective:
        raise click.UsageError("only --effective is currently supported")
    payload = _effective_config_payload()
    if json_output:
        _emit_json(payload)
        return
    for key, value in payload.items():
        if key == "env":
            click.echo("env:")
            for env_key, env_value in value.items():
                click.echo(f"  {env_key}: {env_value}")
        else:
            click.echo(f"{key}: {value}")


@cli.command(name="doctor")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def doctor(json_output: bool) -> None:
    """Check non-interactive PT readiness."""
    checks = []
    project_root = Path(__file__).resolve().parent.parent
    checks.append({
        "name": "pt_version",
        "ok": True,
        "detail": PT_VERSION,
    })
    checks.append({
        "name": "project_root",
        "ok": project_root.exists(),
        "detail": str(project_root),
    })
    checks.append({
        "name": "tracker_db",
        "ok": get_db_path().exists(),
        "detail": str(get_db_path()),
    })
    config = _effective_config_payload()
    checks.append({
        "name": "local_config",
        "ok": config["ok"],
        "detail": config["local_config_error"] or config["local_config_path"],
    })
    memory_ok, memory_db = False, config["memory_db_path"]
    if memory_db:
        try:
            with _open_memory_db_readonly(Path(memory_db)) as conn:
                conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()
            memory_ok, memory_detail = True, memory_db
        except PtJsonError as exc:
            memory_detail = exc.message
    elif config["local_config_error"]:
        # Whether memory is configured can't be told from a file that fails to load.
        memory_detail = f"not checked: {config['local_config_error']}"
    else:
        # An optional integration that is off is not a failure.
        memory_ok, memory_detail = True, f"not configured (optional; see {local_config_path()})"
    checks.append({
        "name": "memory_readonly",
        "ok": memory_ok,
        "detail": memory_detail,
    })
    checks.append({
        "name": "non_interactive_json",
        "ok": True,
        "detail": "memory search/recent/stats/export support read-only JSON/NDJSON without Doppler",
    })
    ok = all(check["ok"] for check in checks)
    payload = {
        "schema_version": PT_DOCTOR_SCHEMA_VERSION,
        "ok": ok,
        "version": PT_VERSION,
        "checks": checks,
        "cron_setup": {
            "recommended_path": f"{project_root / 'pt'}",
            "example": f"cd {project_root} && pt memory recent --since 7d --json",
        },
    }
    if json_output:
        _emit_json(payload)
    else:
        for check in checks:
            status = "ok" if check["ok"] else "fail"
            click.echo(f"{status} {check['name']}: {check['detail']}")
    if not ok:
        raise SystemExit(EXIT_BACKEND_UNAVAILABLE)


# =============================================================================
# Message group (Agent Chat)
# =============================================================================

def _load_chat_config() -> dict:
    """Load Agent Chat config from ~/.claude/agent-chat.env or environment."""
    import os
    # governance: allow-silent SF003: empty means unset; ~/.claude/agent-chat.env may fill these below, and an empty url or key raises ClickException
    config = {
        "url": os.environ.get("AGENT_CHAT_URL", ""),
        "key": os.environ.get("AGENT_CHAT_API_KEY", ""),
        "sender": os.environ.get("AGENT_CHAT_SENDER", ""),
    }
    env_file = Path.home() / ".claude" / "agent-chat.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k == "AGENT_CHAT_URL":
                    config["url"] = v
                elif k == "AGENT_CHAT_API_KEY":
                    config["key"] = v
                elif k == "AGENT_CHAT_SENDER":
                    config["sender"] = v
    if not config["url"] or not config["key"]:
        raise click.ClickException(
            "Agent Chat not configured. Set AGENT_CHAT_URL and AGENT_CHAT_API_KEY "
            "in ~/.claude/agent-chat.env"
        )
    return config


def _chat_api_request(method: str, path: str, config: dict,
                      data: dict | None = None, params: dict | None = None) -> dict:
    """Make an HTTP request to the Agent Chat API. Returns parsed JSON."""
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError
    from urllib.parse import urlencode
    import json as json_mod

    url = config["url"].rstrip("/") + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urlencode(filtered)

    payload = json_mod.dumps(data).encode("utf-8") if data else None
    headers = {"X-API-Key": config["key"]}
    if data:
        headers["Content-Type"] = "application/json"

    req = Request(url, data=payload, headers=headers, method=method)
    try:
        resp = urlopen(req, timeout=15)
        return json_mod.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise click.ClickException(f"Agent Chat API error {e.code}: {body}")
    except (URLError, OSError, TimeoutError) as e:
        raise click.ClickException(f"Agent Chat API unreachable: {e}")


def _known_chat_addresses() -> set | None:
    """Addresses a message can actually reach, or None when that can't be known.

    An address is a project name, so the set of deliverable addresses is the
    set of tracked projects, plus the agents and people listed under [chat]
    addresses in the local config. A recipient is known when its bare name is
    in the set (any machine) or its exact name@machine is. Returns None if the project list is
    unavailable — callers must treat that as "cannot verify", never as
    "invalid". An empty set is a real answer: nothing is reachable. A broken
    local config is an error, not "cannot verify".
    """
    try:
        known = chat_addresses()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        db = DatabaseManager()
        for project in db.get_all_projects():
            pid = (project.get("id") or "").strip()
            if pid:
                known.add(pid)
    # governance: allow-silent SF002: documented "cannot verify" value (see docstring); the send skips address verification rather than block on a DB hiccup, and the warning below says so
    except Exception as exc:  # noqa: BLE001 - never block a send on a DB hiccup
        click.echo(f"⚠ could not verify the recipient address (project list unavailable: {exc})", err=True)
        return None
    return known


def _chat_identity():
    """agent-chat/identity.py, the one rule for Agent Chat addresses (the chat hook uses it too)."""
    import sys as _sys
    agent_chat_root = str(Path(__file__).resolve().parent.parent / "agent-chat")
    if agent_chat_root not in _sys.path:
        _sys.path.insert(0, agent_chat_root)
    import identity
    return identity


def _qualify_chat_address(address: str, machine: str) -> str:
    """`my-app` + `mini` -> `my-app@mini`, ignoring an existing qualifier."""
    project, _ = _split_chat_address(address)
    return _chat_identity().qualify(project, machine)


def _self_machine_name() -> str:
    """This machine's @qualifier ("laptop", "mini"), per agent-chat/identity.py.

    The sender address is always BARE — `identity.project_name_for()` returns a
    basename and never a qualified address — so comparing a qualified recipient
    against the sender string can never tell you whether it names THIS machine.
    identity.machine_name() has to be consulted directly.
    """
    return _chat_identity().machine_name()


def _split_chat_address(address: str):
    """Split `my-app@mini` into ("my-app", "mini"); bare name -> (name, None)."""
    return _chat_identity().split_address(address)


def _resolve_self_address(config: dict | None = None) -> str:
    """This session's Agent Chat address, per agent-chat/identity.address_for.

    Taken from the directory pt was called from (PT_CALLER_CWD). Claude Code
    starts every Bash call in the session's project, so only an explicit
    `cd ../other && pt message send` speaks as another project. Outside any
    project, AGENT_CHAT_SENDER and then the configured sender are the fallbacks.
    """
    identity = _chat_identity()
    resolved = (identity.address_for(os.environ.get("PT_CALLER_CWD", os.getcwd()))
                or identity.configured_sender())
    if resolved:
        return resolved
    # governance: allow-silent SF003: empty means no address; `pt message send` refuses an empty sender with ClickException and `list` then shows the whole board, labelled as such
    fallback = (config or {}).get("sender", "") or os.environ.get("AGENT_CHAT_SENDER", "")
    # The configured sender is held to the same rule: no agent may speak as the human.
    return "" if identity.is_human(fallback) else fallback


def _format_message_line(m: dict) -> str:
    """Format a single message as a pipe-delimited line."""
    sender = m.get("sender", "?")
    recipient = m.get("recipient")
    arrow = f"{sender} -> {recipient}" if recipient else sender
    priority = m.get("priority", "normal")
    body_preview = (m.get("body") or "").replace("\n", " ")[:80]
    ts = m.get("ts", "")
    msg_id = m.get("id", "?")
    reply = f" (reply to #{m['reply_to']})" if m.get("reply_to") else ""
    return f"#{msg_id} | {ts} | {arrow}{reply} | {priority} | {body_preview}"


@click.group(name="message", invoke_without_command=True)
@click.pass_context
def message_group(ctx):
    """Agent Chat — send and read messages between agents.

    \b
    Quick start:
      pt message                           # Recent messages
      pt message send "Deploy looks good"  # Send to chat
      pt message list --limit 10           # List with filters
      pt message list --sender mini-claude # Filter by sender
      pt message send "Check status" --to mini-claude  # Direct message

    The sender is the project pt was run from. AGENT_CHAT_HUMAN, if set in
    your shell's environment (e.g. `export AGENT_CHAT_HUMAN=me` in ~/.zshenv),
    is the person's own address: no agent may send as it. Unset reserves nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(message_list)


@message_group.command(name="send")
@click.argument("text")
@click.option("--to", "recipient", default=None, help="Direct message to a specific agent")
@click.option("--priority", type=click.Choice(["low", "normal", "high", "urgent"]),
              default="normal", help="Message priority")
@click.option("--reply-to", "reply_to", type=int, default=None,
              help="Message ID to reply to")
@click.option("--machine", default=None,
              help="Target one machine specifically, e.g. --to my-app --machine mini")
@click.option("--force", is_flag=True,
              help="Send even if the address is not a known project")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def message_send(text, recipient, priority, reply_to, machine, force, json_output):
    """Send a message to Agent Chat.

    \b
    Examples:
      pt message send "SIW launch prep complete"
      pt message send "Check your inbox" --to mini-claude
      pt message send "Acknowledged" --reply-to 19 --priority high
    """
    import json as json_mod
    config = _load_chat_config()
    sender = _resolve_self_address(config)
    if not sender:
        raise click.ClickException(
            "No Agent Chat address for this session. Identity is resolved at "
            "session start from the project directory; if you are outside a "
            "project, set AGENT_CHAT_SENDER explicitly."
        )

    # Sending to yourself is almost always a mis-addressed message, and it used
    # to succeed silently: every agent shared one machine-global identity, so
    # `--to claude-architect` returned "Sent #92 as claude-architect".
    if machine:
        if not recipient:
            raise click.ClickException("--machine needs --to; it qualifies an address.")
        recipient = _qualify_chat_address(recipient, machine)

    if recipient:
        target_project, target_machine = _split_chat_address(recipient)
        self_project, _ = _split_chat_address(sender)
        # The `recipient == sender` conjunct this used to carry was redundant
        # for a bare address (already implied by the project comparison) and
        # actively harmful for a qualified one: the sender is ALWAYS bare, so
        # `--to project-tracker --machine laptop` rewrote the recipient to
        # `project-tracker@laptop`, which never equals bare `project-tracker`,
        # the guard fell through, and the message went to this very session
        # (#6775). Compare machines instead. `project-tracker@mini` from a
        # laptop session stays allowed — that is a different machine's floor
        # manager, a real peer.
        self_machine = _self_machine_name()
        names_this_machine = (
            target_machine is None or target_machine == self_machine
        )
        if target_project == self_project and names_this_machine:
            raise click.ClickException(
                f"'{recipient}' is this session's own address — that message would "
                f"go nowhere. Use --to <other-project>, or omit --to to broadcast."
            )

        # An unknown address is accepted by the server and delivered to nobody.
        # That is how message #91 was lost: addressed to a name no session
        # listens on, reported as sent. Verify before claiming success.
        known = None if force else _known_chat_addresses()
        if known is not None and target_project not in known and recipient not in known:
            import difflib
            close = difflib.get_close_matches(target_project, sorted(known), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise click.ClickException(
                f"'{recipient}' is not a known address, so this message would be "
                f"accepted and delivered to nobody.{hint}\n"
                f"Addresses are project names. Use --force to send anyway."
            )

    payload = {
        "sender": sender,
        "body": text,
        "priority": priority,
    }
    if recipient:
        payload["to"] = recipient
    if reply_to:
        payload["reply_to"] = reply_to

    result = _chat_api_request("POST", "/send", config, data=payload)

    if json_output:
        print(json_mod.dumps(result, indent=2))
    else:
        target = f" to {recipient}" if recipient else " (broadcast)"
        click.echo(f"Sent #{result.get('id', '?')} as {sender}{target}")


@message_group.command(name="list")
@click.option("--limit", "-n", default=20, type=int, help="Number of messages (default: 20)")
@click.option("--since", default=None, help="Show messages after this ISO timestamp")
@click.option("--sender", default=None, help="Filter by sender")
@click.option("--for", "for_recipient", default=None,
              help="Show messages visible to this agent (broadcasts + DMs). "
                   "Defaults to your own address; use --all for the full board.")
@click.option("--all", "show_all", is_flag=True,
              help="Show all traffic, not just your inbox")
@click.option("--oldest-first", is_flag=True,
              help="Oldest first (default is newest first)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def message_list(limit, since, sender, for_recipient, show_all, oldest_first, json_output):
    """List messages from Agent Chat.

    Defaults to INBOX semantics: your own broadcasts + DMs, newest first.

    \b
    Examples:
      pt message list                        # Your inbox, newest 20
      pt message list --all                  # Everyone's traffic
      pt message list --limit 5              # Newest 5 in your inbox
      pt message list --sender mini-claude   # From mini-claude
      pt message list --for my-app           # Someone else's inbox
      pt message list --since 2026-04-03T00:00:00Z
    """
    import json as json_mod
    config = _load_chat_config()

    # Inbox by default. Without this the default view is the whole board, and
    # a DM addressed to you is indistinguishable from noise.
    if for_recipient is None and not show_all:
        for_recipient = _resolve_self_address(config)

    def _fetch(fetch_limit, order=None):
        params = {"limit": str(fetch_limit)}
        if order:
            params["order"] = order
        if since:
            params["since"] = since
        if sender:
            params["sender"] = sender
        if for_recipient:
            params["for"] = for_recipient
        return _chat_api_request("GET", "/messages", config, params=params)

    # `order=desc` makes the server select the NEWEST rows. Ordering is not
    # cosmetic here: with ASC, `--limit 20` returns the twenty OLDEST messages,
    # which is why the default view sat on 2026-04-02 while #91 arrived in
    # August. Reversing the page client-side does not fix that — it reorders
    # the wrong rows.
    result = _fetch(limit, order="desc")
    messages = result.get("messages", [])

    # Older deployments ignore `order` and still return the oldest page. Detect
    # that (a full page whose newest row is not the board's newest) and recover
    # by pulling a wider window and taking the tail. One extra call, and only
    # against a server that has not been redeployed yet.
    if result.get("order") != "desc" and len(messages) >= limit:
        wide = _fetch(max(limit * 10, 500))
        wide_messages = wide.get("messages", [])
        if len(wide_messages) > len(messages):
            messages = wide_messages[-limit:]
            result = dict(wide, messages=messages)

    if not oldest_first:
        messages = list(reversed(messages))
    result = dict(result, messages=messages)

    if json_output:
        print(json_mod.dumps(result, indent=2))
        return
    if not messages:
        scope = "your inbox" if for_recipient and not show_all else "the board"
        click.echo(f"No messages found in {scope}.")
        return
    for m in messages:
        click.echo(_format_message_line(m))
    click.echo(f"\nMessages: {len(messages)}")


# =============================================================================
# pt skills — skill-invocation leaderboards (#5913)
# =============================================================================

@click.group(name="skills", invoke_without_command=True)
@click.pass_context
def skills_group(ctx):
    """Skill usage leaderboards backed by the memory store's skill_invocations table."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _collect_invocations(**filters):
    """Read-through helper that turns reader errors into click.UsageError."""
    from datetime import datetime, timedelta, timezone

    days = filters.pop("days", 30)
    since = None
    if days and days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        rows = list(_iter_skill_invocations(since=since, **filters))
    except _SkillInvocationsTableMissing as exc:
        raise click.ClickException(str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:  # no store, or a local config that can't be read
        raise click.ClickException(str(exc)) from exc
    return rows, days


def _fmt_last_used(dt):
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


_UNKNOWN_AGENT_LABEL = "unknown"


def _agent_display(caller_type: str) -> str:
    """Render empty caller_type as 'unknown' for humans; everything else raw."""
    return caller_type or _UNKNOWN_AGENT_LABEL


def _agent_query_value(flag_value: str) -> str:
    """Translate the ``--agent`` CLI value into the raw caller_type string.

    Invariant: caller_type is never the literal
    string 'unknown' in the DB — it's either a real attribution ('main',
    'subagent', …) or empty. So mapping the human-friendly 'unknown' token
    to '' is safe and round-trip-unambiguous. Revisit this translation if
    the producer hook ever starts writing 'unknown' as an explicit value.
    """
    return "" if flag_value == _UNKNOWN_AGENT_LABEL else flag_value


@skills_group.command(name="stats")
@click.option("--days", type=int, default=30,
              help="Look-back window in days. 0 = all time.")
@click.option("--skill", "skill_filter", default=None,
              help="Only show rows for one skill name.")
@click.option("--agent", "agent_filter", default=None,
              help="Only show rows for one caller_type. Use 'unknown' to match empty.")
@click.option("--project", "project_filter", default=None,
              help="Only show rows whose cwd resolves to this project label.")
@click.option("--never-used", is_flag=True,
              help="Installed skills with zero invocations in the window.")
@click.option("--by-project", is_flag=True,
              help="Group the leaderboard by project.")
@click.option("--by-agent", is_flag=True,
              help="Group the leaderboard by caller_type (agent identity).")
@click.option("--json", "json_output", is_flag=True,
              help="Emit machine-readable JSON instead of a Rich table.")
def skills_stats(days, skill_filter, agent_filter, project_filter,
                 never_used, by_project, by_agent, json_output):
    """Leaderboard of skill invocations over a time window."""
    import json as json_mod
    from collections import Counter, defaultdict

    render_modes = [m for m, on in (
        ("--never-used", never_used),
        ("--by-project", by_project),
        ("--by-agent", by_agent),
    ) if on]
    if len(render_modes) > 1:
        raise click.UsageError(
            f"{' and '.join(render_modes)} are mutually exclusive — pick one."
        )

    rows, window_days = _collect_invocations(
        skill=skill_filter,
        agent=_agent_query_value(agent_filter) if agent_filter is not None else None,
        project=project_filter,
        days=days,
    )

    if never_used:
        used = {r.skill_name for r in rows}
        installed = _installed_skills()
        missing = sorted(set(installed) - used)
        if json_output:
            print(json_mod.dumps({
                "window_days": window_days,
                "installed_count": len(installed),
                "used_count": len(used),
                "never_used": missing,
            }, indent=2))
            return
        table = Table(title=f"Installed skills never invoked in last {window_days} day(s)",
                      show_header=True, header_style="bold")
        table.add_column("Skill")
        table.add_column("SKILL.md", style="dim")
        for name in missing:
            table.add_row(name, str(installed[name]))
        console.print(table)
        console.print(f"[dim]{len(missing)} never-used of {len(installed)} installed[/dim]")
        return

    if by_project:
        per_project: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            per_project[r.project][r.skill_name] += 1
        if json_output:
            print(json_mod.dumps({
                "window_days": window_days,
                "projects": {p: dict(c) for p, c in per_project.items()},
            }, indent=2))
            return
        for project, counter in sorted(per_project.items()):
            table = Table(title=f"Project: {project}",
                          show_header=True, header_style="bold")
            table.add_column("Skill")
            table.add_column("Calls", justify="right")
            for name, count in counter.most_common():
                table.add_row(name, str(count))
            console.print(table)
        return

    if by_agent:
        per_agent: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            per_agent[_agent_display(r.caller_type)][r.skill_name] += 1
        if json_output:
            print(json_mod.dumps({
                "window_days": window_days,
                "agents": {a: dict(c) for a, c in per_agent.items()},
            }, indent=2))
            return
        for agent, counter in sorted(per_agent.items()):
            table = Table(title=f"Agent: {agent}",
                          show_header=True, header_style="bold")
            table.add_column("Skill")
            table.add_column("Calls", justify="right")
            for name, count in counter.most_common():
                table.add_row(name, str(count))
            console.print(table)
        return

    counter: Counter = Counter()
    last_seen: dict[str, "datetime"] = {}
    for r in rows:
        counter[r.skill_name] += 1
        if r.skill_name not in last_seen or r.invoked_at > last_seen[r.skill_name]:
            last_seen[r.skill_name] = r.invoked_at

    if json_output:
        payload = {
            "window_days": window_days,
            "total_invocations": len(rows),
            "skills": [
                {
                    "skill": name,
                    "calls": count,
                    "last_used": last_seen[name].isoformat(timespec="seconds"),
                }
                for name, count in counter.most_common()
            ],
        }
        print(json_mod.dumps(payload, indent=2))
        return

    window_label = "all time" if window_days == 0 else f"last {window_days} day(s)"
    table = Table(title=f"Skill usage — {window_label}",
                  show_header=True, header_style="bold")
    table.add_column("Skill")
    table.add_column("Calls", justify="right")
    table.add_column("Last used", style="dim")
    for name, count in counter.most_common():
        table.add_row(name, str(count), _fmt_last_used(last_seen[name]))
    console.print(table)
    console.print(f"[dim]{len(rows)} invocation(s) across {len(counter)} skill(s)[/dim]")


# =============================================================================
# pt db — schema migration runner (Phase 2.1a step 2 / §2.2 wrapper)
# =============================================================================


@click.group(name="db", invoke_without_command=True)
@click.pass_context
def db_group(ctx):
    """Database schema migrations.

    The migration runner applies any pending migrations from
    `scripts/db/migrations/` to the local tracker.db. Each migration
    runs inside a transaction, after a verified backup.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@db_group.command(name="migrate")
def db_migrate():
    """Apply every pending migration in order."""
    db = DatabaseManager()

    # Schema migration is a destructive operation (ALTERs, and migration 018
    # drops the cr-sqlite tables and triggers). `authorize` takes and verifies
    # a full timestamped backup first.
    result = db.authorize(
        "migrations_apply", reason="pt db migrate: apply pending schema migrations"
    )
    if not result["ok"]:
        console.print(f"[red]pt db migrate: {result['error']}[/red]")
        sys.exit(2)

    applied = result["applied"]
    if not applied:
        console.print("[green]✓ no pending migrations[/green]")
        return

    console.print(f"[green]✓ applied {len(applied)} migration(s):[/green]")
    for m in applied:
        console.print(f"  • {m['version']:03d}_{m['name']}")


# =============================================================================
# Handoff group — structured unfinished-work / non-PR records (Phase D)
# =============================================================================

PT_HANDOFF_SCHEMA_VERSION = "pt.handoff.v1"


def _get_tracker_db() -> DatabaseManager:
    """Open the local database, preserving structured availability errors."""
    try:
        db = DatabaseManager()
        db.health_snapshot()
    except _LOCAL_DB_ERRORS as exc:
        raise PtJsonError(
            "backend_unavailable",
            f"tracker database unavailable: {exc}",
            EXIT_BACKEND_UNAVAILABLE,
        ) from exc
    return db


_HANDOFF_FILE_CLASSIFICATIONS = frozenset(
    {"committed", "staged", "dirty", "untracked", "discarded"}
)


def _validate_files_array(parsed_files: list) -> None:
    """Validate that --files JSON contains well-formed elements.

    Each element must be a dict with str `path` and str `classification`,
    where classification is one of {committed, staged, dirty, untracked,
    discarded}. Raises PtJsonError on first invalid element.
    """
    for i, element in enumerate(parsed_files):
        if not isinstance(element, dict):
            raise PtJsonError(
                "validation",
                f"--files element {i} must be an object with path/classification",
                EXIT_VALIDATION,
            )
        path = element.get("path")
        classification = element.get("classification")
        if not isinstance(path, str) or not isinstance(classification, str):
            raise PtJsonError(
                "validation",
                f"--files element {i} missing required keys path/classification",
                EXIT_VALIDATION,
            )
        if classification not in _HANDOFF_FILE_CLASSIFICATIONS:
            raise PtJsonError(
                "validation",
                f"--files element {i} classification must be one of "
                f"{sorted(_HANDOFF_FILE_CLASSIFICATIONS)}, got {classification!r}",
                EXIT_VALIDATION,
            )
        # Paths recorded here are later fed to `migration finish --revert`,
        # which trashes untracked entries. Reject anything that isn't a plain
        # repo-relative path at the point of entry, so a bad path is never
        # written down in the first place (#6890). `_contained_path` at the
        # revert site is the backstop for records written before this existed.
        if not path.strip():
            raise PtJsonError(
                "validation",
                f"--files element {i} path is empty",
                EXIT_VALIDATION,
            )
        if Path(path).is_absolute():
            raise PtJsonError(
                "validation",
                f"--files element {i} path must be repo-relative, got absolute {path!r}",
                EXIT_VALIDATION,
            )
        if path.strip() in (".", "..") or ".." in Path(path).parts:
            raise PtJsonError(
                "validation",
                f"--files element {i} path must stay inside the repository, got {path!r}",
                EXIT_VALIDATION,
            )

def _auto_classify_files() -> list[dict]:
    """Run `git status --porcelain` in cwd and return classified file list.

    Surfaces failures via stderr warnings so callers using --auto-files can
    tell "no dirty files" from "git unavailable / repo missing / timeout".
    Returns [] on any failure.
    """
    timeout_seconds = 10
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:  # governance: allow-silent SF002: CLI contract pinned by test_handoff_cli: the stderr warning tells "git unavailable" apart from "no dirty files" and the handoff is still recorded
        click.echo(
            "warning: git not available, --auto-files returning empty list",
            err=True,
        )
        return []
    except subprocess.TimeoutExpired:  # governance: allow-silent SF002: CLI contract pinned by test_handoff_cli: the stderr warning tells a timeout apart from "no dirty files" and the handoff is still recorded
        click.echo(
            f"warning: git status timed out at {timeout_seconds}s, "
            "--auto-files returning empty list",
            err=True,
        )
        return []
    if result.returncode != 0:
        stderr_excerpt = (result.stderr or "").strip().replace("\n", " ")[:200]
        click.echo(
            f"warning: git status returned exit code {result.returncode}, "
            f"stderr: {stderr_excerpt}, --auto-files returning empty list",
            err=True,
        )
        return []
    files: list[dict] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        path = line[3:].strip()
        x, y = xy[0], xy[1]
        if x == "?" and y == "?":
            classification = "untracked"
        elif x in ("A", "M", "R", "C", "D") and y == " ":
            classification = "staged"
        elif y in ("M", "D"):
            classification = "dirty"
        else:
            classification = "dirty"
        files.append({"path": path, "classification": classification})
    return files


def _detect_current_branch() -> str:
    """Return current git branch name, or empty string if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:  # governance: allow-silent SF002: the handoff is still recorded with no branch, like a non-repo cwd; the stderr warning says the lookup failed
        click.echo(f"warning: could not detect the current git branch ({exc}); recording no branch", err=True)
        return ""


@click.group(name="handoff", invoke_without_command=True)
@click.pass_context
def handoff_group(ctx: click.Context) -> None:
    """Record structured unfinished-work or non-PR handoff records."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@handoff_group.command(name="create")
@click.argument("card_id", type=int)
@click.option("--branch", default=None, help="Git branch (defaults to current branch)")
@click.option("--intent", required=True, help="One paragraph: what this work intends to do")
@click.option("--status", "current_status", required=True, help="One paragraph: where work stands now")
@click.option("--next", "next_command", required=True, help="Literal next bash command for the resuming agent")
@click.option("--guidance", "discard_or_preserve_guidance", required=True,
              help="What the resuming agent should do with dirty/staged files")
@click.option("--files", default=None,
              help='JSON array of {path, classification} objects. classification ∈ '
                   '{committed,staged,dirty,untracked,discarded}')
@click.option("--auto-files", is_flag=True,
              help="Auto-classify files from `git status --porcelain`")
@click.option("--type", "record_type", default="unfinished",
              type=click.Choice(["unfinished", "pr_exempt"]),
              help="Record type (default: unfinished)")
@click.option("--reason", default=None, help="Required when --type pr_exempt")
@click.option("--disposition", default=None,
              type=click.Choice(["reverted", "discarded", "left_as_artifact", "merged_elsewhere"]),
              help="Required when --type pr_exempt")
@click.option("--approver", default=None, help="Who approved the pr_exempt record")
@click.option("--json", "json_output", is_flag=True,
              help="Emit the created record as JSON (pt.handoff.v1)")
def handoff_create(
    card_id: int,
    branch: Optional[str],
    intent: str,
    current_status: str,
    next_command: str,
    discard_or_preserve_guidance: str,
    files: Optional[str],
    auto_files: bool,
    record_type: str,
    reason: Optional[str],
    disposition: Optional[str],
    approver: Optional[str],
    json_output: bool,
) -> None:
    """Record unfinished work or a non-PR exit for CARD_ID."""
    # Validate pr_exempt requirements
    if record_type == "pr_exempt":
        if not reason:
            _emit_json_error(
                PtJsonError("validation", "--reason is required when --type pr_exempt", EXIT_VALIDATION),
                "handoff.create",
            )
        if not disposition:
            _emit_json_error(
                PtJsonError(
                    "validation",
                    "--disposition is required when --type pr_exempt",
                    EXIT_VALIDATION,
                ),
                "handoff.create",
            )

    # Resolve file list
    if files and auto_files:
        _emit_json_error(
            PtJsonError("validation", "--files and --auto-files are mutually exclusive", EXIT_VALIDATION),
            "handoff.create",
        )
    if files:
        try:
            parsed_files = json.loads(files)
            if not isinstance(parsed_files, list):
                raise ValueError("must be a JSON array")
        except (json.JSONDecodeError, ValueError) as exc:
            _emit_json_error(
                PtJsonError("validation", f"--files must be a valid JSON array: {exc}", EXIT_VALIDATION),
                "handoff.create",
            )
        try:
            _validate_files_array(parsed_files)
        except PtJsonError as exc:
            _emit_json_error(exc, "handoff.create")
    elif auto_files:
        parsed_files = _auto_classify_files()
    else:
        parsed_files = []

    # Resolve branch
    resolved_branch = branch if branch is not None else _detect_current_branch()

    # Resolve created_by
    resolved_created_by = _resolve_created_by()

    now = datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        db = _get_tracker_db()
    except PtJsonError as exc:
        _emit_json_error(exc, "handoff.create")

    try:
        outcome = db.handoff_create(
            card_id=card_id,
            branch=resolved_branch,
            file_list=json.dumps(parsed_files),
            intent=intent,
            current_status=current_status,
            next_command=next_command,
            discard_or_preserve_guidance=discard_or_preserve_guidance,
            record_type=record_type,
            pr_exempt_reason=reason,
            pr_exempt_disposition=disposition,
            pr_exempt_approver=approver,
            created_at=now,
            created_by=resolved_created_by,
        )
    except _LOCAL_DB_ERRORS as exc:
        _emit_json_error(
            PtJsonError("query_failure", f"handoff create failed: {exc}", EXIT_QUERY_FAILURE),
            "handoff.create",
        )
    if not outcome["ok"]:
        _emit_json_error(
            PtJsonError("validation", outcome["error"], EXIT_VALIDATION),
            "handoff.create",
        )
    record_dict = outcome["record"]

    if json_output:
        _emit_json({
            "schema_version": PT_HANDOFF_SCHEMA_VERSION,
            "ok": True,
            "command": "handoff.create",
            "result": record_dict,
        })
    else:
        click.echo(f"✓ Handoff #{record_dict['id']} created for card #{card_id} ({record_type})")
        click.echo(f"  branch: {resolved_branch or '(none)'}")
        click.echo(f"  intent: {intent[:80]}{'...' if len(intent) > 80 else ''}")
        click.echo(f"  next:   {next_command[:80]}{'...' if len(next_command) > 80 else ''}")


@handoff_group.command(name="list")
@click.option("--card", "card_id", default=None, type=int, help="Filter by card ID")
@click.option("--project", default=None, help="Filter by project name")
@click.option("--unresolved-only", is_flag=True, help="Only show unresolved handoffs")
@click.option("--json", "json_output", is_flag=True, help="Output machine-readable JSON (pt.handoff.v1)")
def handoff_list(
    card_id: Optional[int],
    project: Optional[str],
    unresolved_only: bool,
    json_output: bool,
) -> None:
    """List handoff records with optional filters."""
    try:
        db = _get_tracker_db()
    except PtJsonError as exc:
        _emit_json_error(exc, "handoff.list")

    try:
        records = db.handoff_list(
            card_id=card_id, project=project, unresolved_only=unresolved_only
        )
    except _LOCAL_DB_ERRORS as exc:
        _emit_json_error(
            PtJsonError("query_failure", f"handoff list failed: {exc}", EXIT_QUERY_FAILURE),
            "handoff.list",
        )

    if json_output:
        _emit_json({
            "schema_version": PT_HANDOFF_SCHEMA_VERSION,
            "ok": True,
            "command": "handoff.list",
            "result": records,
        })
        return

    if not records:
        click.echo("No handoffs found.")
        return

    for r in records:
        resolved = r["resolved_at"] or "open"
        click.echo(
            f"#{r['id']} card={r['card_id']} type={r['record_type']} "
            f"resolved={resolved}  {r['intent'][:60]}"
        )


@handoff_group.command(name="show")
@click.argument("handoff_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output machine-readable JSON (pt.handoff.v1)")
def handoff_show(handoff_id: int, json_output: bool) -> None:
    """Show a single handoff record by ID."""
    try:
        db = _get_tracker_db()
    except PtJsonError as exc:
        _emit_json_error(exc, "handoff.show")

    try:
        record = db.handoff_show(handoff_id=handoff_id)
    except _LOCAL_DB_ERRORS as exc:
        _emit_json_error(
            PtJsonError("query_failure", f"handoff show failed: {exc}", EXIT_QUERY_FAILURE),
            "handoff.show",
        )

    if record is None:
        _emit_json_error(
            PtJsonError("validation", f"handoff {handoff_id} does not exist", EXIT_VALIDATION),
            "handoff.show",
        )
    if json_output:
        _emit_json({
            "schema_version": PT_HANDOFF_SCHEMA_VERSION,
            "ok": True,
            "command": "handoff.show",
            "result": record,
        })
        return

    click.echo(f"Handoff #{record['id']}")
    click.echo(f"  card_id:   {record['card_id']}")
    click.echo(f"  project:   {record['project']}")
    click.echo(f"  branch:    {record['branch']}")
    click.echo(f"  type:      {record['record_type']}")
    click.echo(f"  created:   {record['created_at']}")
    click.echo(f"  resolved:  {record['resolved_at'] or 'open'}")
    click.echo(f"\nIntent:\n  {record['intent']}")
    click.echo(f"\nCurrent status:\n  {record['current_status']}")
    click.echo(f"\nNext command:\n  {record['next_command']}")
    click.echo(f"\nGuidance:\n  {record['discard_or_preserve_guidance']}")
    if record["record_type"] == "pr_exempt":
        click.echo(f"\nPR-exempt reason: {record['pr_exempt_reason']}")
        click.echo(f"Disposition:       {record['pr_exempt_disposition']}")
        click.echo(f"Approver:          {record['pr_exempt_approver']}")
    if record["file_list"]:
        click.echo(f"\nFiles ({len(record['file_list'])}):")
        for f in record["file_list"]:
            click.echo(f"  [{f.get('classification', '?')}] {f.get('path', '?')}")


@handoff_group.command(name="resolve")
@click.argument("handoff_id", type=int)
@click.option("--note", default=None, help="Resolution note")
@click.option("--json", "json_output", is_flag=True, help="Output the updated record as JSON (pt.handoff.v1)")
def handoff_resolve(handoff_id: int, note: Optional[str], json_output: bool) -> None:
    """Mark a handoff as resolved."""
    now = datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        db = _get_tracker_db()
    except PtJsonError as exc:
        _emit_json_error(exc, "handoff.resolve")

    try:
        outcome = db.handoff_resolve(handoff_id=handoff_id, resolved_at=now, note=note)
    except _LOCAL_DB_ERRORS as exc:
        _emit_json_error(
            PtJsonError("query_failure", f"handoff resolve failed: {exc}", EXIT_QUERY_FAILURE),
            "handoff.resolve",
        )
    if not outcome["ok"]:
        _emit_json_error(
            PtJsonError("validation", outcome["error"], EXIT_VALIDATION),
            "handoff.resolve",
        )
    record = outcome["record"]

    if json_output:
        _emit_json({
            "schema_version": PT_HANDOFF_SCHEMA_VERSION,
            "ok": True,
            "command": "handoff.resolve",
            "result": record,
        })
    else:
        click.echo(f"✓ Handoff #{handoff_id} resolved at {now}")
        if note:
            click.echo(f"  note: {note}")


# =============================================================================
# pt migration — snapshot-based bulk-operation recording (Phase F)
# =============================================================================
#
# `pt migration start <name>` captures a baseline (git HEAD + porcelain
# status) into a state file at PT_MIGRATION_DIR/<name>.json (default
# ~/.project-tracker/migrations/). `pt migration finish` diffs the current
# working tree against the baseline and PRINTS a manifest (new/modified paths
# plus the header fields). It writes nothing into the repo: an appended
# MIGRATIONS.md made multi-repo sweeps conflict after review had passed.
#
# Optional flags:
#   --commit  marks the session as committed (files left in place)
#   --revert  restores tracked files via `git restore` and trashes
#             untracked files via send2trash (NEVER raw rm)
#
# State file survives reverts because it lives outside the repo. The DB
# row in `migrations` mirrors the lifecycle for cross-session visibility.
# =============================================================================

PT_MIGRATION_SCHEMA_VERSION = "pt.migration.v1"

import re as _re_migration  # noqa: E402  (lazy alias, isolated)

_MIGRATION_NAME_RE = _re_migration.compile(r"^[A-Za-z0-9_-]{1,60}$")


def _migration_state_dir() -> Path:
    """Resolve the directory holding migration state files.

    Respects PT_MIGRATION_DIR for test isolation; otherwise defaults to
    ~/.project-tracker/migrations/. Creates the directory if missing.
    """
    override = os.environ.get("PT_MIGRATION_DIR")
    if override:
        d = Path(override)
    else:
        d = Path.home() / ".project-tracker" / "migrations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migration_state_path(name: str) -> Path:
    return _migration_state_dir() / f"{name}.json"


def _validate_migration_name(name: str) -> None:
    if not _MIGRATION_NAME_RE.match(name):
        raise PtJsonError(
            "validation",
            f"migration name must match [A-Za-z0-9_-]{{1,60}}, got {name!r}",
            EXIT_VALIDATION,
        )


def _caller_repo_root() -> Path:
    """The repo root of the directory the operator actually ran `pt` from.

    `Path.cwd()` is useless here: the `pt` launcher does `cd "$launcher_dir"`
    before exec'ing, so inside pt.py the cwd is ALWAYS project-tracker. That is
    why `pt migration start` recorded the wrong repo for every project (#6851).
    The launcher exports the real directory as PT_CALLER_CWD, which the rest of
    the codebase already reads (_detect_project_from_cwd, scripts/origin.py);
    migration never got the memo.

    PT_CALLER_CWD is the load-bearing part. `git rev-parse --show-toplevel`
    on top of it additionally handles being invoked from a subdirectory of a
    repo, which `Path.cwd()` never did. Falls back to the caller cwd itself
    when that is not a git repo.

    `_find_repo_root()` used to carry this same bug by design — it tried
    `Path(__file__).parent.parent` first — and now delegates here (#6883), so
    this is the single place the caller's repo is resolved.
    """
    caller = Path(os.environ.get("PT_CALLER_CWD") or os.getcwd())
    try:
        stdout, rc = _run_git(caller, ["rev-parse", "--show-toplevel"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return caller
    if rc != 0:
        return caller
    top = stdout.strip()
    return Path(top) if top else caller


def _git_head_sha(repo_dir: Path) -> Optional[str]:
    """Return current HEAD SHA, or None if outside a repo / git missing."""
    try:
        stdout, rc = _run_git(repo_dir, ["rev-parse", "HEAD"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):  # governance: allow-silent SF002: documented "HEAD unknown"; it only feeds the advisory HEAD-drift warning, which is skipped when either side is None
        return None
    if rc != 0:
        return None
    sha = stdout.strip()
    return sha or None


def _git_porcelain_baseline(repo_dir: Path) -> list[str]:
    """Return current `git status --porcelain` lines (verbatim) as baseline.

    A git failure raises PtJsonError. An empty baseline at `migration start`
    would make every pre-existing dirty file look new at finish, and
    `--revert` would then restore or trash it.
    """
    try:
        stdout, rc = _run_git(repo_dir, ["status", "--porcelain"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise PtJsonError(
            "query_failure",
            f"git status --porcelain failed in {repo_dir}: {exc}",
            EXIT_QUERY_FAILURE,
        ) from exc
    if rc != 0:
        return []
    return [line for line in stdout.splitlines() if line.strip()]


def _parse_porcelain_paths(lines: list[str]) -> dict[str, str]:
    """Parse porcelain lines into {path: status} maps.

    Returns a dict keyed by current path (the rename target for renames).
    Value is a 2-char XY status; we tag untracked '??' specially.
    """
    out: dict[str, str] = {}
    for line in lines:
        if len(line) < 4:
            continue
        xy = line[:2]
        rest = line[3:].strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        out[rest] = xy
    return out


def _classify_status(xy: str) -> str:
    """Classify a porcelain XY status into {staged, dirty, untracked}."""
    if xy == "??":
        return "untracked"
    x, y = xy[0], xy[1]
    if x in ("A", "M", "R", "C", "D") and y == " ":
        return "staged"
    return "dirty"


def _diff_porcelain(
    baseline: list[str], current: list[str]
) -> tuple[list[dict], list[dict]]:
    """Diff two porcelain snapshots. Returns (added, modified) classified entries.

    `added` — paths present in current but not baseline.
    `modified` — paths present in both but with a different XY status.
    Both lists contain {path, classification, status} dicts.
    """
    base_map = _parse_porcelain_paths(baseline)
    curr_map = _parse_porcelain_paths(current)
    added: list[dict] = []
    modified: list[dict] = []
    for path, xy in curr_map.items():
        if path not in base_map:
            added.append(
                {"path": path, "classification": _classify_status(xy), "status": xy}
            )
        elif base_map[path] != xy:
            modified.append(
                {"path": path, "classification": _classify_status(xy), "status": xy}
            )
    return added, modified


@click.group(name="jobs", invoke_without_command=True)
@click.pass_context
def jobs_group(ctx: click.Context) -> None:
    """Manage local job listings and submissions."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@jobs_group.command(name="import")
@click.argument("jsonl_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Parse and validate without writing to database")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def jobs_import(jsonl_path: Path, dry_run: bool, json_output: bool) -> None:
    """Import job listings from a JSONL file, one JSON object per line.

    A job-board exporter writes one listing per line; this command
    upserts them into the local database. A bad line rejects only itself:
    the other lines still import and the bad ones are reported. The command
    then exits 1; with --json it exits 0 and the envelope says "ok": false.

    \b
    Required (non-empty strings; null, empty or whitespace counts as missing):
      company, title   trimmed
      url              canonical posting URL, the unique key for upsert
      source           ats_sweep | hn | manual

    \b
    Optional:
      location, posted_date (ISO date), raw   string or null; null = omitted
      category   Frontend/React | Full Stack | Forward Deployed / Solutions |
                 SEO | Backend | Other (default)
      kind       job (default for a new row) | volunteer
    category and kind must be omitted to get the default: an explicit null,
    or any other value, rejects the line. Volunteer rows appear only on the
    dashboard's Volunteer page (/jobs/volunteer), never in Jobs listings,
    stats or submissions. Always include raw when the source has it.

    \b
    Re-import (idempotent by url):
      - updates company, title, location, source, posted_date, category
      - updates kind only when the line supplies it; changing a row to job
        drops its volunteering start date, so it returns to the Jobs page
      - keeps first_seen and deleted_at (a dismissed job stays dismissed),
        and keeps the stored raw when the new raw is null

    \b
    HN "Who's Hiring" comments (free text, often several roles each):
      - required: one line per comment with url {comment_url}#post, title
        "Full HN post (read for every role)" and the full comment text in raw;
        company is best-effort, "Hacker News (company not parsed)" otherwise.
        Omitting it, or cutting its raw text, breaks the contract.
      - best-effort: one line per confidently parsed role, url
        {comment_url}#role-{N} (N from 1, stable for the same text).
        Missing or partial #role-N lines are allowed.

    \b
    Example lines:
      {"company": "Acme Corp", "title": "Senior React Engineer", "url": "https://jobs.acme.com/123", "source": "ats_sweep", "category": "Frontend/React", "raw": "..."}
      {"company": "Example Food Bank", "title": "Website volunteer", "url": "https://volunteer.example.org/roles/42", "source": "manual", "kind": "volunteer", "raw": "..."}
    """
    from db.jobs import JobsMigrationPendingError, validate_import_record
    
    errors: list[dict] = []
    imported: list[dict] = []
    skipped: list[dict] = []
    # Dry run must flag what a real run rejects (#101668605654102016):
    # volunteer lines need migration 016. Checked once, on first need.
    volunteer_supported: bool | None = None
    
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append({
                        "line": line_num,
                        "reason": f"invalid JSON: {exc}",
                        "data": line[:100],
                    })
                    continue
                
                # Type and value checks reject only this line (#101668605654102016).
                try:
                    fields = validate_import_record(record)
                except ValueError as exc:
                    errors.append({"line": line_num, "reason": str(exc), "data": record})
                    continue

                if dry_run:
                    if fields["kind"] == "volunteer":
                        if volunteer_supported is None:
                            volunteer_supported = DatabaseManager().volunteer_kind_supported()
                        if not volunteer_supported:
                            errors.append({
                                "line": line_num,
                                "reason": str(JobsMigrationPendingError()),
                                "data": record,
                            })
                            continue
                    imported.append({"line": line_num, "url": record["url"]})
                    continue
                
                try:
                    db = DatabaseManager()
                    job = db.upsert_job(**fields)
                    imported.append({
                        "line": line_num,
                        "url": record["url"],
                        "job_id": job["id"],
                    })
                except ValueError as exc:
                    errors.append({
                        "line": line_num,
                        "reason": str(exc),
                        "data": record,
                    })
                except Exception as exc:
                    errors.append({
                        "line": line_num,
                        "reason": f"database error: {exc}",
                        "data": record,
                    })
    except Exception as exc:
        if json_output:
            _emit_json({
                "schema_version": PT_JSON_SCHEMA_VERSION,
                "ok": False,
                "command": "jobs.import",
                "error": {"type": "io_error", "message": str(exc)},
            })
        else:
            click.echo(f"Error reading {jsonl_path}: {exc}", err=True)
        sys.exit(1)
    
    if json_output:
        _emit_json({
            "schema_version": PT_JSON_SCHEMA_VERSION,
            "ok": len(errors) == 0,
            "command": "jobs.import",
            "result": {
                "imported": len(imported),
                "errors": len(errors),
                "dry_run": dry_run,
                "details": {
                    "imported": imported,
                    "errors": errors,
                },
            },
        })
    else:
        if dry_run:
            click.echo(f"DRY RUN: Would import {len(imported)} job(s) from {jsonl_path}")
        else:
            click.echo(f"✓ Imported {len(imported)} job(s) from {jsonl_path}")
        
        if errors:
            click.echo(f"✗ {len(errors)} error(s):", err=True)
            for err in errors[:10]:
                click.echo(f"  line {err['line']}: {err['reason']}", err=True)
            if len(errors) > 10:
                click.echo(f"  ... and {len(errors) - 10} more error(s)", err=True)
            sys.exit(1)


@click.group(name="migration", invoke_without_command=True)
@click.pass_context
def migration_group(ctx: click.Context) -> None:
    """Record bulk-operation sessions (start/finish with manifest)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@migration_group.command(name="start")
@click.argument("name")
@click.option("--force", is_flag=True, help="Overwrite an existing unfinished state file")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def migration_start(name: str, force: bool, json_output: bool) -> None:
    """Open a migration recording session named NAME.

    Captures git HEAD and `git status --porcelain` as the baseline. The
    state file is written to PT_MIGRATION_DIR/<name>.json (default
    ~/.project-tracker/migrations/) so it survives `--revert` and
    `trash` operations on the repo.
    """
    try:
        _validate_migration_name(name)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.start")

    state_path = _migration_state_path(name)
    if state_path.exists() and not force:
        _emit_json_error(
            PtJsonError(
                "validation",
                f"migration {name!r} already has an unfinished state file at "
                f"{state_path}; pass --force to overwrite or run "
                f"`pt migration finish {name}` first",
                EXIT_VALIDATION,
            ),
            "migration.start",
        )

    repo_dir = _caller_repo_root()
    head_sha = _git_head_sha(repo_dir)
    try:
        baseline = _git_porcelain_baseline(repo_dir)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.start")
    now = datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved_created_by = _resolve_created_by()

    state = {
        "schema_version": PT_MIGRATION_SCHEMA_VERSION,
        "name": name,
        "repo_dir": str(repo_dir),
        "started_at": now,
        "baseline_head": head_sha,
        "baseline_porcelain": baseline,
        "created_by": resolved_created_by,
    }
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    # Best-effort DB row (skipped silently if tracker DB unavailable —
    # the state file is the source of truth for revert).
    db_id: Optional[int] = None
    try:
        outcome = _get_tracker_db().migration_session_start(
            name=name,
            started_at=now,
            baseline_head=head_sha,
            created_by=resolved_created_by,
        )
        if outcome["ok"]:
            db_id = outcome["id"]
        else:
            # Best-effort by design: the state file is the source of truth for
            # revert, so a database failure must not abort the command. It used
            # to be `except sqlite3.Error: pass`, which meant the row could
            # vanish with nothing said. Say it.
            click.echo(
                f"⚠ migration session not recorded in the tracker DB: {outcome['error']}",
                err=True,
            )
    except (PtJsonError, *_LOCAL_DB_ERRORS) as exc:
        click.echo(f"⚠ migration session not recorded in the tracker DB: {exc}", err=True)

    if json_output:
        _emit_json({
            "schema_version": PT_MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "command": "migration.start",
            "result": {
                "name": name,
                "state_path": str(state_path),
                "started_at": now,
                "baseline_head": head_sha,
                "baseline_files": len(baseline),
                "db_id": db_id,
            },
        })
    else:
        click.echo(f"✓ Migration {name!r} started at {now}")
        click.echo(f"  state: {state_path}")
        click.echo(f"  HEAD:  {head_sha or '(no HEAD)'}")
        click.echo(f"  baseline: {len(baseline)} dirty/untracked path(s)")


def _build_manifest(
    name: str,
    state: dict,
    finished_at: str,
    added: list[dict],
    modified: list[dict],
    action: str,
    head_drift_warning: Optional[str],
) -> dict:
    """The manifest of a finished migration session, as structured data."""
    return {
        "name": name,
        "started_at": state.get("started_at"),
        "finished_at": finished_at,
        "baseline_head": state.get("baseline_head"),
        "action": action,
        "head_drift_warning": head_drift_warning,
        "added": added,
        "modified": modified,
    }


def _render_manifest(manifest: dict) -> str:
    """Render a `_build_manifest` dict as a markdown section."""
    lines: list[str] = [
        f"## {manifest['name']} — {manifest['finished_at']}",
        "",
        f"- started_at:  `{manifest['started_at']}`",
        f"- finished_at: `{manifest['finished_at']}`",
        f"- baseline_head: `{manifest['baseline_head'] or '(none)'}`",
        f"- action: `{manifest['action']}`",
    ]
    if manifest["head_drift_warning"]:
        lines.append(f"- WARNING: {manifest['head_drift_warning']}")
    for title, entries in (
        ("New paths (introduced during session)", manifest["added"]),
        ("Modified paths (status changed during session)", manifest["modified"]),
    ):
        lines.append("")
        lines.append(f"### {title}")
        if entries:
            for entry in entries:
                lines.append(f"- `[{entry['classification']}]` `{entry['path']}`")
        else:
            lines.append("- _(none)_")
    return "\n".join(lines)


class PathEscapesRepoError(ValueError):
    """A recorded path does not resolve to a location inside its repository."""


def _audit_destructive(operation: str, target: Path, repo_dir: Path) -> None:
    """Record an in-process deletion before it happens.

    The `bash-validator` hook logs destructive *shell* commands, but it cannot
    see a `send2trash()` call inside a running Python process. That blind spot
    is not academic: when a project directory was sent to the Trash while
    its developers were working in it, there was no shell history and no hook
    log to attribute it, because the deletion happened inside `pt` (#6890).

    Anything in this codebase that deletes should record it here first, so the
    next incident is a `grep` rather than an interrogation. Logging must never
    prevent the operation, but a failure is surfaced rather than swallowed —
    a silently broken audit trail is the thing being fixed.
    """
    from datetime import datetime, timezone

    # PT_DESTRUCTIVE_LOG_PATH lets tests redirect this. Without it every test
    # run that touches a delete site writes synthetic pytest-tmp entries into
    # the real log — 51 of 51 entries were test chaff before this existed,
    # which buries genuine evidence in exactly the file meant to preserve it.
    log_path = Path(
        os.environ.get("PT_DESTRUCTIVE_LOG_PATH")
        or Path.home() / ".claude" / "logs" / "pt-destructive.log"
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "target": str(target),
            "repo_dir": str(repo_dir),
            "caller_cwd": os.environ.get("PT_CALLER_CWD", os.getcwd()),
            "pid": os.getpid(),
            "session_id": os.environ.get("CLAUDE_SESSION_ID"),
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 — never block the operation
        print(f"warning: could not write destructive-op audit log: {exc}", file=sys.stderr)


def _contained_path(repo_dir: Path, raw: str) -> Path:
    """Resolve `raw` inside `repo_dir`, refusing anything that escapes it.

    This is a destructive-operation guard, not a formatting nicety. Without it
    `repo_dir / raw` is trivially escapable, because pathlib discards the
    left-hand side when the right-hand side is absolute:

        Path("repo") / "an/absolute/path"  ->  that absolute path, repo discarded
        Path("repo") / ""                  ->  repo   (the root itself)
        Path("repo") / "."                 ->  repo   (the root itself)
        Path("repo") / "../sibling"        ->  repo/../sibling

    A migration revert carrying such a path could send any directory on the
    machine to the Trash from inside an unrelated repo. That is not
    hypothetical — it is what trashed a project directory while its
    developers were working, and it left no shell history and no hook log
    because the deletion happened inside a Python call in another repo's
    process (#6890).

    Resolution is done with `.resolve()` on both sides so symlinked paths
    cannot be used to step outside either.

    Raises PathEscapesRepoError for absolute paths, `..` escapes, empty/`.`
    paths that name the repository root itself, and symlink escapes.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathEscapesRepoError("path is empty")

    # A NUL byte makes `.resolve()`'s lstat raise a bare ValueError, which would
    # escape the PathEscapesRepoError handlers and abort the whole revert
    # instead of skipping just this entry. Refuse it as what it is.
    if "\x00" in raw:
        raise PathEscapesRepoError(f"path contains a NUL byte: {raw!r}")

    candidate = Path(raw)
    if candidate.is_absolute():
        raise PathEscapesRepoError(f"path is absolute: {raw!r}")

    root = repo_dir.resolve()
    try:
        target = (root / candidate).resolve()
    except (ValueError, OSError) as exc:
        # Anything else the filesystem refuses to resolve is, by definition,
        # not a path we can prove is inside the repo.
        raise PathEscapesRepoError(f"path could not be resolved: {raw!r} ({exc})") from exc

    if target == root:
        raise PathEscapesRepoError(f"path resolves to the repository root: {raw!r}")
    if not target.is_relative_to(root):
        raise PathEscapesRepoError(f"path escapes the repository: {raw!r}")
    return target


def _revert_paths(
    repo_dir: Path, entries: list[dict]
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Revert paths discovered between start and finish.

    Tracked paths (classification staged/dirty) → `git restore --` per path.
    Untracked paths → send2trash. Already-missing untracked files are
    reported as "already missing" rather than erroring.

    Every path is checked with `_contained_path` before anything destructive
    happens. State files written before that guard existed may carry absolute
    or escaping paths, so validating only at the `--files` entry point is not
    enough — the check has to sit at the destructive boundary too.

    Returns (restored, trashed, errors) where errors is [(path, reason)].
    """
    restored: list[str] = []
    trashed: list[str] = []
    errors: list[tuple[str, str]] = []

    def _safe(entries_in: list[dict]) -> list[dict]:
        """Drop entries that escape the repo, recording why."""
        kept = []
        for entry in entries_in:
            try:
                _contained_path(repo_dir, entry["path"])
            except PathEscapesRepoError as exc:
                errors.append((str(entry.get("path")), f"refused: {exc}"))
                continue
            kept.append(entry)
        return kept

    tracked = _safe([e for e in entries if e["classification"] in ("staged", "dirty")])
    untracked = _safe([e for e in entries if e["classification"] == "untracked"])

    # Restore tracked files (one-shot — git restore takes a list).
    if tracked:
        paths = [e["path"] for e in tracked]
        try:
            stdout, rc = _run_git(repo_dir, ["restore", "--"] + paths)
            if rc == 0:
                restored.extend(paths)
            else:
                # Best-effort per-path fallback so one bad path doesn't
                # block the others.
                for p in paths:
                    _, rc1 = _run_git(repo_dir, ["restore", "--", p])
                    if rc1 == 0:
                        restored.append(p)
                    else:
                        errors.append((p, f"git restore exit {rc1}"))
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            errors.extend((p, f"git restore failed: {exc}") for p in paths)

    # Trash untracked files via send2trash (NEVER raw rm).
    if untracked:
        try:
            from send2trash import send2trash  # type: ignore
        except ImportError as exc:
            for e in untracked:
                errors.append((e["path"], f"send2trash unavailable: {exc}"))
            return restored, trashed, errors

        for e in untracked:
            # Re-derive through the guard rather than reusing repo_dir / path:
            # this is the line that actually deletes, so it resolves its own
            # target rather than trusting an earlier check (#6890).
            try:
                target = _contained_path(repo_dir, e["path"])
            except PathEscapesRepoError as exc:
                errors.append((str(e.get("path")), f"refused: {exc}"))
                continue
            if not target.exists():
                errors.append((e["path"], "already missing"))
                continue
            try:
                _audit_destructive("send2trash", target, repo_dir)
                send2trash(str(target))
                trashed.append(e["path"])
            except Exception as exc:  # noqa: BLE001 — trash backend can raise OSError variants
                errors.append((e["path"], f"send2trash failed: {exc}"))

    return restored, trashed, errors


@migration_group.command(name="finish")
@click.argument("name")
@click.option("--commit", "commit_flag", is_flag=True, help="Mark session as committed (leaves files in place)")
@click.option("--revert", "revert_flag", is_flag=True, help="Restore tracked files; trash untracked files")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def migration_finish(
    name: str, commit_flag: bool, revert_flag: bool, json_output: bool
) -> None:
    """Close migration NAME and print its manifest of touched paths.

    The manifest is printed (or, with --json, returned under "manifest");
    nothing is written into the repo. Without --commit or --revert, files
    are left in place. --commit and --revert are mutually exclusive.
    """
    try:
        _validate_migration_name(name)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.finish")

    if commit_flag and revert_flag:
        _emit_json_error(
            PtJsonError(
                "validation",
                "--commit and --revert are mutually exclusive",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    state_path = _migration_state_path(name)
    if not state_path.exists():
        _emit_json_error(
            PtJsonError(
                "validation",
                f"no state file for migration {name!r} at {state_path}; "
                f"run `pt migration start {name}` first",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _emit_json_error(
            PtJsonError(
                "validation",
                f"state file at {state_path} is unreadable: {exc}",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    caller_root = _caller_repo_root()
    repo_dir = Path(state.get("repo_dir") or caller_root)

    # Every state file written before #6851 carries project-tracker as its
    # repo_dir, whatever repo the operator was actually in. Today that is
    # mostly benign — baseline and current were both computed against the
    # wrong tree, so the diff comes out empty — but `--revert` runs
    # `git restore` with cwd=repo_dir and trashes `repo_dir / path`. Acting
    # on a stale state file would be a destructive operation against the
    # wrong worktree, so refuse rather than guess which one is right.
    if revert_flag and repo_dir.resolve() != caller_root.resolve():
        _emit_json_error(
            PtJsonError(
                "validation",
                f"refusing --revert: state file records repo_dir {repo_dir}, "
                f"but this session's repo root is {caller_root}. Reverting "
                f"would run `git restore` and trash files in a different tree. "
                f"Re-run `pt migration finish {name}` from {repo_dir}, or "
                f"finish without --revert and undo by hand.",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    baseline_head: Optional[str] = state.get("baseline_head")
    current_head = _git_head_sha(repo_dir)
    head_drift_warning: Optional[str] = None
    if baseline_head and current_head and current_head != baseline_head:
        head_drift_warning = (
            f"HEAD drifted from baseline {baseline_head[:12]} "
            f"to {current_head[:12]}; revert will restore against current HEAD"
        )

    baseline = state.get("baseline_porcelain", []) or []
    try:
        current = _git_porcelain_baseline(repo_dir)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.finish")
    added, modified = _diff_porcelain(baseline, current)

    finished_at = datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Decide action label.
    if revert_flag:
        action = "reverted"
    elif commit_flag:
        action = "committed"
    else:
        action = "manifest-only"

    revert_summary: dict = {"restored": [], "trashed": [], "errors": []}
    if revert_flag:
        # Revert both added (new in session) and modified (changed in session).
        all_entries = added + modified
        restored, trashed, errors = _revert_paths(repo_dir, all_entries)
        revert_summary = {
            "restored": restored,
            "trashed": trashed,
            "errors": [{"path": p, "reason": r} for p, r in errors],
        }

    manifest = _build_manifest(
        name, state, finished_at, added, modified, action, head_drift_warning
    )

    # Update DB row if present.
    status_value = {
        "manifest-only": "finished",
        "committed": "committed",
        "reverted": "reverted",
    }[action]
    try:
        outcome = _get_tracker_db().migration_session_finish(
            name=name,
            finished_at=finished_at,
            status=status_value,
        )
        if not outcome["ok"]:
            # Best-effort, but not silent — see migration start.
            click.echo(
                f"⚠ migration session not closed in the tracker DB: {outcome['error']}",
                err=True,
            )
    except (PtJsonError, *_LOCAL_DB_ERRORS) as exc:
        click.echo(f"⚠ migration session not closed in the tracker DB: {exc}", err=True)

    # Remove the state file so the name is reusable.
    try:
        state_path.unlink()  # governance: allow-delete DS001: pt's own state file from `migration start`; its content is in the printed manifest
    except OSError as exc:
        # The session is finished and its manifest printed, but a leftover state
        # file would let a second `finish` replay the stale baseline. Say so.
        click.echo(f"⚠ could not remove migration state file {state_path}: {exc}", err=True)

    if json_output:
        _emit_json({
            "schema_version": PT_MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "command": "migration.finish",
            "result": {
                "name": name,
                "finished_at": finished_at,
                "action": action,
                "manifest": manifest,
                "added": added,
                "modified": modified,
                "head_drift_warning": head_drift_warning,
                "revert_summary": revert_summary,
            },
        })
        return

    click.echo(f"✓ Migration {name!r} finished at {finished_at}")
    click.echo(f"  action:   {action}")
    click.echo(f"  added:    {len(added)}  modified: {len(modified)}")
    if head_drift_warning:
        click.echo(f"  WARNING: {head_drift_warning}")
    if revert_flag:
        click.echo(f"  restored: {len(revert_summary['restored'])} tracked path(s)")
        click.echo(f"  trashed:  {len(revert_summary['trashed'])} untracked path(s)")
        if revert_summary["errors"]:
            click.echo(f"  errors:   {len(revert_summary['errors'])}")
            for e in revert_summary["errors"]:
                click.echo(f"    - {e['path']}: {e['reason']}")
    click.echo("")
    click.echo(_render_manifest(manifest))


@migration_group.command(name="list")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def migration_list(json_output: bool) -> None:
    """List migration sessions recorded in the tracker DB."""
    try:
        db = _get_tracker_db()
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.list")

    try:
        records = db.migration_session_list()
    except _LOCAL_DB_ERRORS as exc:
        _emit_json_error(
            PtJsonError("query_failure", f"migration list failed: {exc}", EXIT_QUERY_FAILURE),
            "migration.list",
        )

    if json_output:
        _emit_json({
            "schema_version": PT_MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "command": "migration.list",
            "result": records,
        })
        return

    if not records:
        click.echo("No migrations recorded.")
        return
    for r in records:
        finished = r["finished_at"] or "open"
        click.echo(
            f"#{r['id']:>3} {r['name']:<30} {r['status']:<10} "
            f"started={r['started_at']} finished={finished}"
        )


# =============================================================================
# pt size: codebase size, snapshots, optimization score (#8083)
# =============================================================================


def _size_db() -> "DatabaseManager":
    """DatabaseManager with migration 017 confirmed, checked before any scan."""
    db = DatabaseManager()
    try:
        db.codebase_baseline()
    except Exception as exc:  # governance: allow-silent SF001: only "no such table" is mapped; anything else is re-raised
        if "no such table" in str(exc):
            raise click.ClickException(
                "table codebase_size_snapshots is missing; run `pt db migrate`"
            ) from exc
        raise
    return db


def _size_score_text(score: Optional[float]) -> str:
    # Adding 0.0 turns a rounded -0.0 into 0.0, so a change too small to show
    # never reads as a regression.
    return "-" if score is None else f"{round(score, 1) + 0.0:+.1f}"


@click.command(name="size")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON")
@click.option("--snapshot", is_flag=True, help="Append this scan as a new run (stored only if every repo scanned)")
@click.pass_context
def size_command(ctx: click.Context, json_output: bool, snapshot: bool) -> None:
    """Codebase size per project and optimization score vs the baseline.

    score = (baseline_lean - current_lean) / baseline_lean * 100, where
    lean = non-test code lines + markdown doc lines and the baseline is the
    repo's row in the earliest `pt size --snapshot` run. Positive = leaner.
    Each repo's checked-out working tree is measured, best effort.
    """
    from scripts import codebase_size as cs
    from scripts.config import projects_root

    db = _size_db()
    root = projects_root()
    try:
        rows = cs.scan_portfolio(root)
    except cs.ProjectsRootMissing as exc:
        raise click.ClickException(str(exc)) from exc
    if not rows:
        raise click.ClickException(f"no git repos found under {root}")
    good = [r for r in rows if not r.error]
    failed = [r for r in rows if r.error]

    today = datetime.now().date().isoformat()
    # A snapshot run is all-or-nothing: a run with a repo missing would not be
    # one complete measurement (and the first run becomes the baseline).
    stored = bool(snapshot and not failed)
    if stored:
        db.save_codebase_scan(good, today)
    # Read after saving, so the very first snapshot is its own baseline.
    base_date, base, _, _ = db.codebase_runs()

    report = cs.build_report(good, base, base_date)
    scored = report["rows"]
    total = report["total"]
    total_score = total["score"]

    if json_output:
        click.echo(json.dumps({
            "baseline_date": base_date,
            "snapshot_stored": stored,
            "rows": scored,
            "total": total,
            "formula": report["formula"],
            "errors": [{"project": r.project, "error": r.error} for r in failed],
        }, indent=2))
    else:
        click.echo(
            f"baseline: {base_date}" if base_date
            else "baseline: none yet (the first `pt size --snapshot` run becomes it)"
        )
        click.echo(
            f"{'code':>7} {'tests':>7} {'docs':>5} {'doclines':>8} {'last':>10} "
            f"{'c90':>4} {'score':>7}  project"
        )
        for d in scored:
            click.echo(
                f"{d['code']:>7} {d['tests']:>7} {d['doc_files']:>5} {d['doc_lines']:>8} "
                f"{d['last_commit'] or '-':>10} {d['commits_90d'] if d['commits_90d'] is not None else '-':>4} "
                f"{_size_score_text(d['score']):>7}  {d['project']}"
            )
        click.echo(
            f"TOTAL code {total['code']} tests {total['tests']} docs {total['doc_files']} "
            f"doclines {total['doc_lines']} repos {total['repos']} "
            f"score {_size_score_text(total_score)}"
        )
        if stored:
            click.echo(f"Stored {len(good)} scan rows for {today}.")
    for r in failed:
        click.echo(f"ERROR {r.project}: {r.error}", err=True)
    if snapshot and not stored:
        click.echo(
            f"Snapshot NOT stored: {len(failed)} repo(s) failed to scan. "
            "Fix them and re-run `pt size --snapshot`.",
            err=True,
        )
    if failed:
        ctx.exit(1)


# =============================================================================
# pt reminders: the dashboard's Reminders page (#8282)
# =============================================================================


def _reminders(call: Callable[["DatabaseManager"], Any]) -> Any:
    """Run one reminders call; a database without migration 022 says how to fix it."""
    try:
        return call(DatabaseManager())
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            raise click.ClickException("table reminders is missing; run `pt db migrate`") from exc
        raise


@click.group(name="reminders", invoke_without_command=True)
@click.pass_context
def reminders_group(ctx: click.Context) -> None:
    """Things you want to remember: tools, guides, research, tips, decisions.

    They show on the dashboard's Reminders page (/reminders), one
    line each. Keep one until its content is built into a process, skill or
    tool; whoever builds it in marks it built in. With no subcommand, lists
    the active ones.

    \b
    Add one (agents: this is the command):
      pt reminders add "Prompt pack" \\
        --purpose "Run a client's work audit, then evals, skills and a schedule" \\
        --location ~/library/prompt-pack/
    """
    if ctx.invoked_subcommand is not None:
        return
    reminders = _reminders(lambda db: db.list_reminders())
    for r in reminders["active"]:
        click.echo(f"#{r['id']} {r['name']}  (added {r['created_at'][:10]})")
        for value in (r["purpose"], r["location"]):
            if value:
                click.echo(f"    {value}")
    if not reminders["active"]:
        click.echo("No active reminders.")
    click.echo(f"{len(reminders['built_in'])} built in.")


@reminders_group.command(name="add")
@click.argument("name")
@click.option("--purpose", help="Why or when to reach for it")
@click.option("--location", help="Where it lives: a path or a link")
def reminders_add(name: str, purpose: Optional[str], location: Optional[str]) -> None:
    """Add a reminder; it appears on the dashboard page on the next load."""
    try:
        reminder = _reminders(lambda db: db.add_reminder(name, purpose, location))
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="NAME") from exc
    click.echo(f"Added reminder #{reminder['id']}: {reminder['name']}")


@reminders_group.command(name="built-in")
@click.argument("reminder_id", type=int)
def reminders_built_in(reminder_id: int) -> None:
    """Retire a reminder whose content is now part of a process, skill or tool.

    The agent that builds a reminder into something marks it here, so the
    page keeps showing only what is still worth remembering.
    """
    reminder = _reminders(lambda db: db.mark_reminder_built_in(reminder_id))
    if reminder is None:
        raise click.ClickException(f"no reminder #{reminder_id}")
    click.echo(f"Reminder #{reminder_id} marked built in: {reminder['name']}")


# =============================================================================
# Register subgroups and run
# =============================================================================

cli.add_command(backup_group)
cli.add_command(tasks_group)
cli.add_command(calendar_group)
cli.add_command(memory_group)
cli.add_command(graph_group)
cli.add_command(worktrees_group)
cli.add_command(info_group)
cli.add_command(config_group)
cli.add_command(message_group)
cli.add_command(skills_group)
cli.add_command(db_group)
cli.add_command(handoff_group)
cli.add_command(jobs_group)
cli.add_command(size_command)
cli.add_command(reminders_group)
cli.add_command(migration_group)

def main() -> NoReturn:
    """Run the CLI and report database availability failures with the documented exit code."""
    try:
        cli(standalone_mode=False)
    except _LOCAL_DB_ERRORS as exc:
        click.echo(f"pt: {exc}", err=True)
        sys.exit(EXIT_BACKEND_UNAVAILABLE)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
