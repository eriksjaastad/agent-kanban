"""pt info: the centralized key-value reference store."""

import click
from db.manager import DatabaseManager


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
