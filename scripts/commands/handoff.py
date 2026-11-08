"""pt handoff: structured unfinished-work and non-PR records (Phase D)."""

import json
import subprocess
from datetime import datetime
from datetime import timezone as _timezone
from pathlib import Path
from typing import Optional

import click
from commands.common import (
    _LOCAL_DB_ERRORS,
    EXIT_BACKEND_UNAVAILABLE,
    EXIT_QUERY_FAILURE,
    EXIT_VALIDATION,
    PtJsonError,
    _emit_json,
    _emit_json_error,
)
from db.manager import DatabaseManager
from origin import resolve_created_by as _resolve_created_by

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


_HANDOFF_FILE_CLASSIFICATIONS = frozenset({"committed", "staged", "dirty", "untracked"})


def _validate_files_array(parsed_files: list) -> None:
    """Validate that --files JSON contains well-formed elements.

    Each element must be a dict with str `path` and str `classification`,
    where classification is one of {committed, staged, dirty, untracked}. Raises PtJsonError on first invalid element.
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

def _git(args: list[str]) -> str:
    """git stdout in cwd. A missing git, a timeout or a non-zero exit is an error:
    every recorded handoff (162 by 2026-11-01) had a branch, so nothing is
    recorded with a guessed-empty one."""
    try:
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PtJsonError("query_failure", f"git {' '.join(args)} failed: {exc}", EXIT_QUERY_FAILURE) from exc
    if result.returncode != 0:
        raise PtJsonError("query_failure", f"git {' '.join(args)} exited {result.returncode}: "
                          f"{result.stderr.strip()[:200]} (pass --branch/--files to skip git)", EXIT_QUERY_FAILURE)
    return result.stdout


def _auto_classify_files() -> list[dict]:
    """Classify `git status --porcelain` in cwd."""
    files: list[dict] = []
    for line in _git(["status", "--porcelain"]).splitlines():
        x, y, path = line[0], line[1], line[3:].strip()
        if x == "?" and y == "?":
            classification = "untracked"
        elif x in ("A", "M", "R", "C", "D") and y == " ":
            classification = "staged"
        else:
            classification = "dirty"
        files.append({"path": path, "classification": classification})
    return files


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
                   '{committed,staged,dirty,untracked}')
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
    try:
        if not files:
            parsed_files = _auto_classify_files() if auto_files else []
        resolved_branch = branch if branch is not None else _git(["branch", "--show-current"]).strip()
    except PtJsonError as exc:
        _emit_json_error(exc, "handoff.create")

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
