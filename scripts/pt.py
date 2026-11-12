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
import sys
import webbrowser
import time
from datetime import datetime
from pathlib import Path
from typing import NoReturn, Optional
import subprocess

import click
from rich.markup import escape
from rich.progress import Progress

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from scripts.addon_registry import enabled as enabled_addons, load as load_addon_part
from scripts.config import (PROJECTS_BASE_DIR, local_config_path)
from db.schema import init_db, get_db_path
from db.manager import DatabaseManager
from db.checkouts import MACHINE_LABELS, CheckoutsMigrationPendingError, ProjectCheckedOut
from discovery.project_scanner import (
    discover_projects,
    extract_project_metadata,
    scan_health_parallel,
)
from discovery.external_resources_parser import parse_external_resources
from discovery.graph_builder import GraphBuilder
# update_directory_index removed — 00_Index generation killed (#5530)
from commands.addons import addons_group  # noqa: E402
from commands.backup import backup_group  # noqa: E402
from commands.calendar import calendar_group  # noqa: E402
from commands.common import (  # noqa: E402
    _checkout_label,
    console,
    _emit_json,
    _emit_json_error,
    EXIT_BACKEND_UNAVAILABLE,
    EXIT_HYGIENE_FINDING,
    EXIT_VALIDATION,
    _LOCAL_DB_ERRORS,
    PT_DOCTOR_SCHEMA_VERSION,
    PT_HYGIENE_SCHEMA_VERSION,
    PT_VERSION,
    PtJsonError,
    _resolve_project_id,
    _run_git,
)
from commands.config import config_group, _effective_config_payload  # noqa: E402
from commands.db import db_group  # noqa: E402
from commands.graph import graph_group  # noqa: E402
from commands.handoff import handoff_group  # noqa: E402
from commands.info import info_group  # noqa: E402
from commands.jobs import jobs_group  # noqa: E402
from commands.memory import memory_group, _open_memory_db_readonly  # noqa: E402
from commands.message import message_group  # noqa: E402
from commands.record_run import record_run_group  # noqa: E402
from commands.site_stats import site_stats_group  # noqa: E402
from commands.skills import skills_group  # noqa: E402
from commands.tasks import tasks_group  # noqa: E402
from commands.worktrees import worktrees_group  # noqa: E402
from commands.migration import (  # noqa: E402
    _audit_destructive,
    migration_group,
)


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
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON (schema: pt.hygiene.v2)")
@click.option("--project", "project_name", default=None, help="Inspect only one repo by name")
@click.option("--quiet", is_flag=True, help="Only show repos with at least one finding")
def hygiene(json_output: bool, project_name: Optional[str], quiet: bool) -> None:
    """Check portfolio-wide git hygiene state. Read-only.

    \b
    pt hygiene --json                        portfolio-wide scan, JSON
    pt hygiene --json --project my-project   one repo
    pt hygiene --quiet                       human output, findings only

    Detects: dirty working tree (PROGRESS.md excluded), local-only branches,
    branches ahead of remote, stashes, and a stale PROGRESS.md (over 7 days
    old while the repo has uncommitted work). A git command that fails or
    times out makes that repo a scan error, never a clean result.

    Exit 0 = all repos clean; exit 6 = at least one finding (for cron
    branching).

    --json emits schema pt.hygiene.v2. Each entry under "results" has one of
    two disjoint shapes:

    \b
    - scanned:  {project, path, clean, findings: {dirty_tree,
                local_only_branches, branches_ahead_of_remote, stashes,
                stale_progress_md}}  (all five keys present; each finding
                has "present")
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
        "stale_progress_md": _hygiene_stale_progress_md(repo_dir),
    }
    clean = not any(v["present"] for v in findings.values())
    return {
        "project": repo_dir.name,
        "path": str(repo_dir),
        "clean": clean,
        "findings": findings,
    }


def _hygiene_git(repo_dir: Path, args: list[str]) -> str:
    """git stdout; a non-zero exit raises (a timeout already does), so the repo
    becomes a scan error instead of reading as clean."""
    stdout, rc = _run_git(repo_dir, args)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} exited {rc} in {repo_dir}")
    return stdout


def _hygiene_non_progress_dirty_files(repo_dir: Path) -> list[str]:
    """Return dirty file paths from `git status --porcelain`, excluding PROGRESS.md.

    Shared by `_hygiene_dirty_tree` (for the finding) and
    `_hygiene_stale_progress_md` (to decide whether the repo has uncommitted
    work other than PROGRESS.md). A git failure raises, so `pt hygiene`
    records the repo as a scan error instead of reporting it clean.
    """
    dirty_files: list[str] = []
    for line in _hygiene_git(repo_dir, ["status", "--porcelain"]).splitlines():
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
    local_only = []
    for line in _hygiene_git(repo_dir, ["branch", "--format=%(refname:short) %(upstream)"]).splitlines():
        branch, upstream = line.split(" ", 1)
        # Skip main/master/trunk — they may not have upstreams in some setups
        if branch in ("main", "master", "trunk"):
            continue
        if not upstream.strip():
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
    ahead_branches = []
    refs = _hygiene_git(repo_dir, ["for-each-ref", "--format=%(refname:short)|%(upstream:short)", "refs/heads"])
    for line in refs.splitlines():
        branch, upstream = line.split("|", 1)
        if not upstream:
            # No upstream -> handled by local_only_branches finding instead
            continue
        count_out, count_rc = _run_git(repo_dir, ["rev-list", "--count", f"{upstream}..{branch}"])
        if count_rc != 0:
            # The upstream ref is gone (deleted on the remote, then pruned).
            continue
        ahead = int(count_out)
        if ahead > 0:
            ahead_branches.append({"name": branch, "ahead": ahead})

    return {"present": bool(ahead_branches), "branches": ahead_branches}


def _hygiene_stashes(repo_dir: Path) -> dict:
    """Detect stashes (count > 0)."""
    count = len(_hygiene_git(repo_dir, ["stash", "list"]).splitlines())
    return {"present": count > 0, "count": count}


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
# Config / Doctor — non-interactive automation diagnostics
# =============================================================================


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
cli.add_command(record_run_group)
cli.add_command(site_stats_group)
cli.add_command(migration_group)
cli.add_command(addons_group)
for _addon in enabled_addons():
    for _command in _addon.commands:
        cli.add_command(load_addon_part(_command))

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
