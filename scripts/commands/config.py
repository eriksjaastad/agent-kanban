"""pt config: show the effective local configuration and where each value came from."""

import os
from pathlib import Path

import click
from commands.common import PT_CONFIG_SCHEMA_VERSION, _emit_json
from db.schema import get_db_path

from scripts.config import (
    PROJECTS_BASE_DIR,
    chat_addresses,
    digest_integration,
    fit_keywords,
    graph_settings,
    user_facing_projects,
    local_config,
    local_config_path,
    memory_integration,
    morning_integration,
    tailor_rules,
)


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
        user_facing_projects(data)
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
        "project_root": str(Path(__file__).resolve().parents[2]),
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
