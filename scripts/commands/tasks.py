"""pt tasks: create, list, update and walk cards across the board."""

import json
import sys
from datetime import datetime
from pathlib import Path

import click
from commands.common import (
    _blocked_by_report,
    _blocked_state,
    _checkout_label,
    _checkout_marker,
    _detect_project_from_cwd,
    _display_tasks,
    _format_task_ref,
    _format_task_refs,
    _has_complete_prompt,
    _parse_blocked_by,
    _print_checkin_reminders,
    _resolve_project_id,
    _resolve_task_id,
    console,
)
from db.checkouts import MACHINE_LABELS
from db.manager import DatabaseManager
from origin import resolve_created_by as _resolve_created_by
from rich.markup import escape
from rich.table import Table
from utils.validation import BlockedTaskProjectError


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
