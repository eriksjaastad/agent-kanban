"""pt size: codebase size per project, snapshots, and the optimization score (#8083)."""

import json
from datetime import datetime
from typing import Optional

import click
from db.manager import DatabaseManager


def _size_db() -> "DatabaseManager":
    """DatabaseManager with migration 017 confirmed, checked before any scan."""
    db = DatabaseManager()
    try:
        db.codebase_baseline()
    except Exception as exc:  # governance: allow-silent SF001: only "no such table" is mapped; anything else is re-raised
        if "no such table" in str(exc):
            raise click.ClickException(
                "table codebase_size_snapshots is missing; run `pt db migrate`"
            ) from exc
        raise
    return db


def _size_score_text(score: Optional[float]) -> str:
    # Adding 0.0 turns a rounded -0.0 into 0.0, so a change too small to show
    # never reads as a regression.
    return "-" if score is None else f"{round(score, 1) + 0.0:+.1f}"


@click.command(name="size")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON")
@click.option("--snapshot", is_flag=True, help="Append this scan as a new run (stored only if every repo scanned)")
@click.pass_context
def size_command(ctx: click.Context, json_output: bool, snapshot: bool) -> None:
    """Codebase size per project and optimization score vs the baseline.

    score = (baseline_lean - current_lean) / baseline_lean * 100, where
    lean = non-test code lines + markdown doc lines and the baseline is the
    repo's row in the earliest `pt size --snapshot` run. Positive = leaner.
    Each repo's checked-out working tree is measured, best effort.
    """
    from scripts import codebase_size as cs
    from scripts.config import projects_root

    db = _size_db()
    root = projects_root()
    try:
        rows = cs.scan_portfolio(root)
    except cs.ProjectsRootMissing as exc:
        raise click.ClickException(str(exc)) from exc
    if not rows:
        raise click.ClickException(f"no git repos found under {root}")
    good = [r for r in rows if not r.error]
    failed = [r for r in rows if r.error]

    today = datetime.now().date().isoformat()
    # A snapshot run is all-or-nothing: a run with a repo missing would not be
    # one complete measurement (and the first run becomes the baseline).
    stored = bool(snapshot and not failed)
    if stored:
        db.save_codebase_scan(good, today)
    # Read after saving, so the very first snapshot is its own baseline.
    base_date, base, _, _ = db.codebase_runs()

    report = cs.build_report(good, base, base_date)
    scored = report["rows"]
    total = report["total"]
    total_score = total["score"]

    if json_output:
        click.echo(json.dumps({
            "baseline_date": base_date,
            "snapshot_stored": stored,
            "rows": scored,
            "total": total,
            "formula": report["formula"],
            "errors": [{"project": r.project, "error": r.error} for r in failed],
        }, indent=2))
    else:
        click.echo(
            f"baseline: {base_date}" if base_date
            else "baseline: none yet (the first `pt size --snapshot` run becomes it)"
        )
        click.echo(
            f"{'code':>7} {'tests':>7} {'docs':>5} {'doclines':>8} {'last':>10} "
            f"{'c90':>4} {'score':>7}  project"
        )
        for d in scored:
            click.echo(
                f"{d['code']:>7} {d['tests']:>7} {d['doc_files']:>5} {d['doc_lines']:>8} "
                f"{d['last_commit'] or '-':>10} {d['commits_90d'] if d['commits_90d'] is not None else '-':>4} "
                f"{_size_score_text(d['score']):>7}  {d['project']}"
            )
        click.echo(
            f"TOTAL code {total['code']} tests {total['tests']} docs {total['doc_files']} "
            f"doclines {total['doc_lines']} repos {total['repos']} "
            f"score {_size_score_text(total_score)}"
        )
        if stored:
            click.echo(f"Stored {len(good)} scan rows for {today}.")
    for r in failed:
        click.echo(f"ERROR {r.project}: {r.error}", err=True)
    if snapshot and not stored:
        click.echo(
            f"Snapshot NOT stored: {len(failed)} repo(s) failed to scan. "
            "Fix them and re-run `pt size --snapshot`.",
            err=True,
        )
    if failed:
        ctx.exit(1)
