"""pt db: the schema migration runner's command line."""

import sys

import click
from commands.common import console
from db.manager import DatabaseManager


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

    # Schema migration is a destructive operation (ALTERs and table rebuilds).
    # `authorize` takes and verifies a full timestamped backup first.
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
