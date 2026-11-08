"""pt skills: skill-invocation leaderboards (#5913)."""

from datetime import datetime

import click
from commands.common import console
from rich.table import Table
from skill_invocations_reader import (
    SkillInvocationsTableMissing as _SkillInvocationsTableMissing,
)
from skill_invocations_reader import iter_invocations as _iter_skill_invocations
from skills_registry import installed_skills as _installed_skills


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
