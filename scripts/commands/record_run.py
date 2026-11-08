"""pt record-run: record scheduled-job runs in loop_executions (#8847).

launchd keeps only each job's last exit, so the dashboard cannot say how many
runs per day needed the owner. Each scheduled job's plist runs its command through
scripts/record-run.sh, which calls `start` before the job and `finish` after
it. The wrapper runs the job whether or not these succeed, so a broken pt or
database never stops a scheduled job; a run still marked running was killed
or its finish could not be recorded.
"""

from datetime import datetime

import click
from db.manager import DatabaseManager


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@click.group(name="record-run")
def record_run_group() -> None:
    """Record scheduled-job runs (used by scripts/record-run.sh)."""


@record_run_group.command(name="start")
@click.argument("label")
def record_run_start(label: str) -> None:
    """Record a run of LABEL as started; prints the run id for `finish`."""
    click.echo(DatabaseManager().loop_run_started(label, _now()))


@record_run_group.command(name="finish")
@click.argument("run_id", type=int)
@click.argument("exit_code", type=int)
def record_run_finish(run_id: int, exit_code: int) -> None:
    """Close run RUN_ID: success on exit 0, else failed with "exit N"."""
    DatabaseManager().loop_run_finished(run_id, _now(), exit_code)
