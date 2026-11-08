"""pt calendar: events, deadlines and their linked cards."""

import json
import sys
from datetime import datetime

import click
from commands.common import (
    _LOCAL_DB_ERRORS,
    EXIT_BACKEND_UNAVAILABLE,
    EXIT_VALIDATION,
    _resolve_project_id,
    console,
)
from db.backend_calendar_manager import validate_event_date, validate_event_time
from db.manager import DatabaseManager


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
@click.option("--end", "end_time", default=None, callback=_time_option,
              help="End: HH:MM, after --time; an event with both is a block for `pt calendar now`")
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
def calendar_add(title, event_date, event_time, end_time, event_type, project,
                 prompt, description, notify_before_minutes, recurrence, json_output):
    """Add a calendar event.

    \b
    Examples:
        pt calendar add "Ship v2" --date 2026-03-28 --type milestone
        pt calendar add "Weekly review" --date 2026-03-25 --time 10:00 --recurrence weekly
        pt calendar add "Check cards" --date 2026-03-24 --prompt "Review all In Progress cards"
        pt calendar add "Deep work" --date 2026-11-02 --time 09:00 --end 11:00 --recurrence daily
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
            end_time=end_time,
            event_type=event_type,
            project_id=project_id,
            prompt=prompt,
            description=description,
            notify_before_minutes=notify_before_minutes,
            recurrence=recurrence,
            created_by="human",
        )
    except ValueError as e:
        click.echo(f"pt calendar add: {e}", err=True)
        sys.exit(EXIT_VALIDATION)
    if json_output:
        print(json_lib.dumps({"id": event_id, "title": title, "event_date": event_date}))
    else:
        console.print(f"[green]✅ Created event #{event_id}: {title} on {event_date}[/green]")


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
@click.option("--end", "end_time", default=None, callback=_time_option, help='New end: HH:MM, after the time; --end "" clears it')
@click.option("-t", "--type", "event_type", default=None)
@click.option("-p", "--project", default=None)
@click.option("--prompt", default=None)
@click.option("--description", default=None)
@click.option("--notify", "notify_before_minutes", default=None, type=int)
def calendar_update(event_id, title, event_date, event_time, end_time, event_type, project,
                    prompt, description, notify_before_minutes):
    """Update any fields on a calendar event."""
    db = DatabaseManager()
    cm = _get_calendar_manager()
    updates = {}
    if title is not None:                 updates["title"] = title
    if event_date is not None:            updates["event_date"] = event_date
    if event_time is not None:            updates["event_time"] = event_time
    if end_time is not None:              updates["end_time"] = end_time
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
        found = cm.update_event(event_id, **updates)
    except ValueError as e:
        click.echo(f"pt calendar update: {e}", err=True)
        sys.exit(EXIT_VALIDATION)
    if found:
        console.print(f"[green]Updated event #{event_id}[/green]")
    else:
        console.print(f"[red]Event #{event_id} not found[/red]")


def _local_now() -> datetime:
    """This machine's wall-clock time, which event times are written in."""
    return datetime.now()


def _block_json(event):
    """One block for `pt calendar now --json`: start and end in this machine's local time."""
    if event is None:
        return None
    return {
        "id": event["id"], "title": event["title"], "project": event.get("project_id"),
        "start": event["start"].isoformat(timespec="seconds"),
        "end": event["end"].isoformat(timespec="seconds"),
    }


@calendar_group.command(name="now")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def calendar_now(json_output):
    """Show the block you are in now and the next one.

    A block is an active event with both a time and an --end, on its date or
    on every day its --recurrence repeats it. Overlapping blocks resolve to the
    one that started last.

    \b
    --json prints, with exit 0 even when there is no block:
      {"schema_version": "pt.calendar.now.v1",
       "current": {"id", "title", "project", "start", "end"} or null,
       "next":    {"id", "title", "project", "start", "end"} or null}
    start and end are ISO 8601 in this machine's local time, with no UTC
    offset; project may be null. A failure exits non-zero with the message
    on stderr only.
    """
    try:
        blocks = _get_calendar_manager().blocks_at(_local_now())
    except _LOCAL_DB_ERRORS as exc:
        click.echo(f"pt calendar now: {exc}", err=True)
        sys.exit(EXIT_BACKEND_UNAVAILABLE)
    current, upcoming = (_block_json(blocks[k]) for k in ("current", "next"))
    if json_output:
        click.echo(json.dumps({"schema_version": "pt.calendar.now.v1", "current": current, "next": upcoming}))
        return
    for label, block in (("Now", current), ("Next", upcoming)):
        if block is None:
            click.echo(f"{label}: no block")
        else:
            span = f"{block['start'][:16].replace('T', ' ')}-{block['end'][11:16]}"
            project = f" ({block['project']})" if block["project"] else ""
            click.echo(f"{label}: {block['title']}{project} {span}")
