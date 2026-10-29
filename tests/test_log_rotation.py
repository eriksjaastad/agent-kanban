"""Tests for log rotation (#6750).

logs/ had grown to 155MB with no rotation anywhere in the repo:
project_tracker.log at 85MB (Python logging) plus dashboard.stderr.log and
dashboard.stdout.log at 23MB and 10MB (written straight to the launchd fd by
uvicorn, so Python logging cannot cap them).
"""

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
LAUNCH_SCRIPT = REPO_ROOT / "scripts" / "launch-dashboard.sh"

sys.path.insert(0, str(REPO_ROOT))


def test_file_logging_uses_a_rotating_handler():
    import scripts.logger as pt_logger

    # pytest injects its own /dev/null handler on the root logger, so inspect
    # the handler list this module actually configures.
    file_handlers = [
        h for h in pt_logger.handlers if isinstance(h, logging.FileHandler)
    ]
    assert file_handlers, "expected a file handler on the root logger"
    for handler in file_handlers:
        assert isinstance(handler, RotatingFileHandler), (
            f"{handler} is unbounded — project_tracker.log grew to 85MB this way"
        )
        assert handler.maxBytes > 0
        assert handler.backupCount > 0

    assert pt_logger.LOG_MAX_BYTES == 10 * 1024 * 1024
    assert pt_logger.LOG_BACKUP_COUNT == 3


def _rotate(log_dir: Path, max_bytes: int = 100, backups: int = 2) -> subprocess.CompletedProcess:
    """Run the launcher's rotation step without launching uvicorn."""
    return subprocess.run(
        ["bash", "-c", f'source "{LAUNCH_SCRIPT}"; rotate_dashboard_logs'],
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(log_dir),
            "PT_DASHBOARD_LOG_DIR": str(log_dir),
            "PT_DASHBOARD_LOG_MAX_BYTES": str(max_bytes),
            "PT_DASHBOARD_LOG_BACKUPS": str(backups),
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def test_oversized_dashboard_log_is_copy_truncated(tmp_path: Path):
    stderr_log = tmp_path / "dashboard.stderr.log"
    stderr_log.write_text("x" * 500)
    inode_before = stderr_log.stat().st_ino

    _rotate(tmp_path)

    # Truncated in place: launchd holds an fd on this inode, so rotation must
    # not rename the file out from under it.
    assert stderr_log.stat().st_size == 0
    assert stderr_log.stat().st_ino == inode_before
    assert (tmp_path / "dashboard.stderr.log.1").read_text() == "x" * 500


def test_small_dashboard_log_is_left_alone(tmp_path: Path):
    stdout_log = tmp_path / "dashboard.stdout.log"
    stdout_log.write_text("still small")

    _rotate(tmp_path)

    assert stdout_log.read_text() == "still small"
    assert not (tmp_path / "dashboard.stdout.log.1").exists()


def test_backups_shift_and_oldest_is_dropped(tmp_path: Path):
    stdout_log = tmp_path / "dashboard.stdout.log"
    stdout_log.write_text("newest" * 100)
    (tmp_path / "dashboard.stdout.log.1").write_text("previous")
    (tmp_path / "dashboard.stdout.log.2").write_text("ancient")

    _rotate(tmp_path)

    assert stdout_log.stat().st_size == 0
    assert (tmp_path / "dashboard.stdout.log.1").read_text() == "newest" * 100
    assert (tmp_path / "dashboard.stdout.log.2").read_text() == "previous"
    # Only LOG_BACKUPS generations are kept.
    assert not (tmp_path / "dashboard.stdout.log.3").exists()


def test_rotation_is_a_noop_when_logs_are_absent(tmp_path: Path):
    result = _rotate(tmp_path)

    assert result.returncode == 0
    # The isolated database fixture creates _local.
    remaining = [p for p in tmp_path.iterdir() if p.name != "_local"]
    assert not remaining


def test_rotation_failure_does_not_block_startup(tmp_path: Path):
    """A failed rotation must warn and continue, never abort the launcher.

    `launch-dashboard.sh` runs under `set -euo pipefail`. Before the guard,
    an unguarded cp/mv failure here aborted the script before uvicorn was
    exec'd — and with launchd KeepAlive that converts "logs are too big" into
    a restart loop, which is the failure that grew stderr to 213MB to begin
    with. Housekeeping must never be able to take the dashboard down.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stderr_log = log_dir / "dashboard.stderr.log"
    stderr_log.write_text("x" * 200)

    # Make the directory unwritable so creating the .1 backup fails, while the
    # existing log file itself stays writable — which is precisely the shape
    # that makes an unguarded rotation destructive: the copy cannot be made,
    # but the truncate would still succeed.
    os.chmod(log_dir, 0o555)

    result = subprocess.run(
        ["bash", "-c", f'source "{LAUNCH_SCRIPT}"; rotate_dashboard_logs; echo REACHED_END'],
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(log_dir),
            "PT_DASHBOARD_LOG_DIR": str(log_dir),
            "PT_DASHBOARD_LOG_MAX_BYTES": "100",
            "PT_DASHBOARD_LOG_BACKUPS": "2",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    try:
        assert "REACHED_END" in result.stdout, (
            "rotation failure aborted the launcher; uvicorn would never start"
        )
        assert result.returncode == 0
        # The log it could not back up must still be intact, not truncated.
        assert stderr_log.read_text() == "x" * 200, (
            "log was truncated despite the backup copy failing"
        )
    finally:
        # Restore permissions so pytest can clean tmp_path up.
        os.chmod(log_dir, 0o755)
