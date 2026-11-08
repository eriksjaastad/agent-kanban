"""pt backup: snapshots, offsite copies, pruning and restores."""

import json
import subprocess
from pathlib import Path

import click
from commands.common import _LOCAL_DB_ERRORS, console
from db.manager import DatabaseManager
from discovery.backup_reader import get_backup_status


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

    An upload is not proof of recovery. To prove one, copy a snapshot down into
    an empty directory, check it with `sqlite3 <file> 'PRAGMA integrity_check'`,
    and read a few cards with `PT_DB_PATH=<file> pt tasks`. Never test a restore
    by replacing the live database.
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
