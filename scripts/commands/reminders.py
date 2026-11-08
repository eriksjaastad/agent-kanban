"""pt reminders: the dashboard's Reminders page from the command line (#8282)."""

import sqlite3
from typing import Any, Callable, Optional

import click
from db.manager import DatabaseManager


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
