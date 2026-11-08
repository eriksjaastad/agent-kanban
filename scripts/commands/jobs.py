"""pt jobs: import job and volunteer listings from JSONL."""

import json
import sys
from pathlib import Path

import click
from commands.common import PT_JSON_SCHEMA_VERSION, _emit_json
from db.manager import DatabaseManager


@click.group(name="jobs", invoke_without_command=True)
@click.pass_context
def jobs_group(ctx: click.Context) -> None:
    """Manage local job listings and submissions."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@jobs_group.command(name="import")
@click.argument("jsonl_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Parse and validate without writing to database")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def jobs_import(jsonl_path: Path, dry_run: bool, json_output: bool) -> None:
    """Import job listings from a JSONL file, one JSON object per line.

    A job-board exporter writes one listing per line; this command
    upserts them into the local database. A bad line rejects only itself:
    the other lines still import and the bad ones are reported. The command
    then exits 1; with --json it exits 0 and the envelope says "ok": false.

    \b
    Required (non-empty strings; null, empty or whitespace counts as missing):
      company, title   trimmed
      url              canonical posting URL, the unique key for upsert
      source           ats_sweep | hn | manual

    \b
    Optional:
      location, posted_date (ISO date), raw   string or null; null = omitted
      category   Frontend/React | Full Stack | Forward Deployed / Solutions |
                 SEO | Backend | Other (default)
      kind       job (default for a new row) | volunteer
    category and kind must be omitted to get the default: an explicit null,
    or any other value, rejects the line. Volunteer rows appear only on the
    dashboard's Volunteer page (/jobs/volunteer), never in Jobs listings,
    stats or submissions. Always include raw when the source has it.

    \b
    Re-import (idempotent by url):
      - updates company, title, location, source, posted_date, category
      - updates kind only when the line supplies it; changing a row to job
        drops its volunteering start date, so it returns to the Jobs page
      - keeps first_seen and deleted_at (a dismissed job stays dismissed),
        and keeps the stored raw when the new raw is null

    \b
    HN "Who's Hiring" comments (free text, often several roles each):
      - required: one line per comment with url {comment_url}#post, title
        "Full HN post (read for every role)" and the full comment text in raw;
        company is best-effort, "Hacker News (company not parsed)" otherwise.
        Omitting it, or cutting its raw text, breaks the contract.
      - best-effort: one line per confidently parsed role, url
        {comment_url}#role-{N} (N from 1, stable for the same text).
        Missing or partial #role-N lines are allowed.

    \b
    Example lines:
      {"company": "Acme Corp", "title": "Senior React Engineer", "url": "https://jobs.acme.com/123", "source": "ats_sweep", "category": "Frontend/React", "raw": "..."}
      {"company": "Example Food Bank", "title": "Website volunteer", "url": "https://volunteer.example.org/roles/42", "source": "manual", "kind": "volunteer", "raw": "..."}
    """
    from db.jobs import JobsMigrationPendingError, validate_import_record
    
    errors: list[dict] = []
    imported: list[dict] = []
    skipped: list[dict] = []
    # Dry run must flag what a real run rejects (#101668605654102016):
    # volunteer lines need migration 016. Checked once, on first need.
    volunteer_supported: bool | None = None
    
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append({
                        "line": line_num,
                        "reason": f"invalid JSON: {exc}",
                        "data": line[:100],
                    })
                    continue
                
                # Type and value checks reject only this line (#101668605654102016).
                try:
                    fields = validate_import_record(record)
                except ValueError as exc:
                    errors.append({"line": line_num, "reason": str(exc), "data": record})
                    continue

                if dry_run:
                    if fields["kind"] == "volunteer":
                        if volunteer_supported is None:
                            volunteer_supported = DatabaseManager().volunteer_kind_supported()
                        if not volunteer_supported:
                            errors.append({
                                "line": line_num,
                                "reason": str(JobsMigrationPendingError()),
                                "data": record,
                            })
                            continue
                    imported.append({"line": line_num, "url": record["url"]})
                    continue
                
                try:
                    db = DatabaseManager()
                    job = db.upsert_job(**fields)
                    imported.append({
                        "line": line_num,
                        "url": record["url"],
                        "job_id": job["id"],
                    })
                except ValueError as exc:
                    errors.append({
                        "line": line_num,
                        "reason": str(exc),
                        "data": record,
                    })
                except Exception as exc:
                    errors.append({
                        "line": line_num,
                        "reason": f"database error: {exc}",
                        "data": record,
                    })
    except Exception as exc:
        if json_output:
            _emit_json({
                "schema_version": PT_JSON_SCHEMA_VERSION,
                "ok": False,
                "command": "jobs.import",
                "error": {"type": "io_error", "message": str(exc)},
            })
        else:
            click.echo(f"Error reading {jsonl_path}: {exc}", err=True)
        sys.exit(1)
    
    if json_output:
        _emit_json({
            "schema_version": PT_JSON_SCHEMA_VERSION,
            "ok": len(errors) == 0,
            "command": "jobs.import",
            "result": {
                "imported": len(imported),
                "errors": len(errors),
                "dry_run": dry_run,
                "details": {
                    "imported": imported,
                    "errors": errors,
                },
            },
        })
    else:
        if dry_run:
            click.echo(f"DRY RUN: Would import {len(imported)} job(s) from {jsonl_path}")
        else:
            click.echo(f"✓ Imported {len(imported)} job(s) from {jsonl_path}")
        
        if errors:
            click.echo(f"✗ {len(errors)} error(s):", err=True)
            for err in errors[:10]:
                click.echo(f"  line {err['line']}: {err['reason']}", err=True)
            if len(errors) > 10:
                click.echo(f"  ... and {len(errors) - 10} more error(s)", err=True)
            sys.exit(1)
