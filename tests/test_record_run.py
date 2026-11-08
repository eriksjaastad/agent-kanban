"""scripts/record-run.sh + pt record-run (#8847): each scheduled-job run becomes a loop_executions row,
and the job always runs with its own exit code, whatever happens to pt or the database."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from commands import record_run  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "scripts" / "record-run.sh"


@pytest.fixture
def setup(tmp_path):
    """A scratch database, and a pt that runs this checkout's pt.py on it."""
    db_path = tmp_path / "test.db"
    create_database(db_path)
    fake_pt = tmp_path / "pt"
    fake_pt.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{REPO / "scripts" / "pt.py"}" "$@"\n')
    fake_pt.chmod(0o755)
    env = {**os.environ, "PT_DB_PATH": str(db_path), "PT_BIN": str(fake_pt)}
    return DatabaseManager(db_path), env, tmp_path


def _wrap(env, cwd, *command):
    return subprocess.run([str(WRAPPER), "local.demo", "--", *command], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def _runs(db):
    conn = db._tracker_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT status, error_message, started_at, completed_at FROM loop_executions"
            " WHERE loop_name = 'local.demo' ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.parametrize("command, exit_code, status, error", [
    (["true"], 0, "success", None),
    (["sh", "-c", "exit 3"], 3, "failed", "exit 3"),
    (["no-such-program-8847"], 127, "failed", "exit 127"),
    (["sh", "-c", "kill -9 $$"], 137, "failed", "exit 137"),
])
def test_each_run_is_recorded_with_the_job_exit_code(setup, command, exit_code, status, error):
    db, env, tmp = setup
    result = _wrap(env, tmp, *command)
    assert result.returncode == exit_code
    [run] = _runs(db)
    assert (run["status"], run["error_message"]) == (status, error)
    assert run["started_at"] <= run["completed_at"] and run["started_at"][-6] in "+-"
    assert db.loop_last_executions(["local.demo"])["local.demo"]["status"] == status


def test_the_job_runs_in_its_own_directory_with_its_own_output(setup):
    _, env, tmp = setup
    result = _wrap(env, tmp, "sh", "-c", "pwd; echo oops >&2")
    assert Path(result.stdout.strip()).resolve() == tmp.resolve()  # nothing of pt's mixed in
    assert "oops" in result.stderr.splitlines()  # pt warns about the scratch database path here too


def test_a_broken_pt_still_runs_the_job_and_keeps_its_exit_code(setup):
    _, env, tmp = setup
    env["PT_BIN"] = str(tmp / "missing-pt")
    marker = tmp / "ran"
    result = _wrap(env, tmp, "sh", "-c", f"touch {marker}; exit 2")
    assert result.returncode == 2 and marker.exists()
    assert "could not record the start of local.demo" in result.stderr


def test_a_failed_finish_is_reported_and_the_run_stays_running(setup):
    db, env, tmp = setup
    # A pt whose start works and whose finish fails, as a database that went away mid-run would.
    fake_pt = Path(env["PT_BIN"])
    real = fake_pt.read_text().splitlines()[1]
    fake_pt.write_text(f'#!/bin/sh\n[ "$2" = finish ] && exit 1\n{real}\n')
    result = _wrap(env, tmp, "true")
    assert result.returncode == 0
    assert "could not record the end of local.demo" in result.stderr
    assert [r["status"] for r in _runs(db)] == ["running"]


def test_finish_closes_the_named_run(setup):
    db, _, _ = setup
    run_id = db.loop_run_started("local.demo", record_run._now())
    db.loop_run_finished(run_id, record_run._now(), 0)
    assert [r["status"] for r in _runs(db)] == ["success"]


@pytest.mark.parametrize("args", [["local.demo", "true"], ["local.demo", "--"], ["local.demo"]])
def test_a_call_without_the_separator_or_a_command_is_refused(setup, args):
    db, env, tmp = setup
    result = subprocess.run([str(WRAPPER), *args], env=env, cwd=tmp, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and "usage: record-run.sh" in result.stderr
    assert _runs(db) == []


def test_a_stalled_pt_delays_the_job_by_at_most_its_bound(setup, monkeypatch):
    _, env, tmp = setup
    stalled = tmp / "stalled-pt"
    stalled.write_text("#!/bin/sh\nsleep 30\n")  # a child holding the output open, as uv's python does
    stalled.chmod(0o755)
    env["PT_BIN"] = str(stalled)
    script = tmp / "fast-bound.sh"  # the wrapper with a 1 s bound, so the test stays quick
    script.write_text(WRAPPER.read_text().replace("' 60 \"$@\"", "' 1 \"$@\""))
    assert script.read_text() != WRAPPER.read_text()
    script.chmod(0o755)
    result = subprocess.run([str(script), "local.demo", "--", "sh", "-c", "exit 4"], env=env, cwd=tmp,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 4
    assert "could not record the start of local.demo" in result.stderr
