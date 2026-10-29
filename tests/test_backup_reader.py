from __future__ import annotations

from datetime import datetime, timedelta, timezone
import plistlib
import pytest
from pathlib import Path

from scripts.discovery import backup_reader


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _touch_with_age(path: Path, age: timedelta, size: int = 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = (datetime.now(timezone.utc) - age).timestamp()
    path.touch()
    import os

    os.utime(path, (ts, ts))


def _iso_age(age: timedelta) -> str:
    return (datetime.now(timezone.utc) - age).isoformat()


def test_get_backup_status_reports_healthy_local_and_cloud(tmp_path, monkeypatch):
    backup_dir = tmp_path / "full-backups"
    log_path = tmp_path / "backup.log"
    launch_agent_path = tmp_path / "LaunchAgents" / "local.project-tracker.backup.plist"
    rclone_config_path = tmp_path / "rclone.conf"

    _touch_with_age(backup_dir / "tracker_20260423_141355.db", timedelta(hours=2))
    _write(
        log_path,
        "\n".join(
            [
                f"{_iso_age(timedelta(hours=2))} | backup | 3510272 bytes | /tmp/tracker_20260423_141355.db",
                f"{_iso_age(timedelta(hours=2))} | cloud_copy | success | b2:tracker-backups/tracker_daily_20260423.db",
            ]
        ),
    )
    launch_agent_path.parent.mkdir(parents=True, exist_ok=True)
    with launch_agent_path.open("wb") as handle:
        plistlib.dump({"Label": "local.project-tracker.backup", "StartInterval": 21600}, handle)
    _write(rclone_config_path, "[b2]\ntype = b2\n")

    monkeypatch.setenv("PT_EXTERNAL_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("PT_BACKUP_LOG_PATH", str(log_path))
    monkeypatch.setenv("PT_BACKUP_LAUNCH_AGENT_PATH", str(launch_agent_path))
    monkeypatch.setenv("PT_BACKUP_RCLONE_DEST", "b2:tracker-backups")
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_config_path))

    status = backup_reader.get_backup_status()

    assert status["status"] == "healthy"
    assert status["local_full"]["count"] == 1
    assert status["cloud"]["status"] == "healthy"
    assert status["cloud"]["configured_dest"] == "b2:tracker-backups"
    assert status["launch_agent"]["schedule"] == "Every 6h"
    assert status["remotes"] == ["b2"]


@pytest.mark.parametrize(
    ("create_dir", "state", "message_part"),
    [
        pytest.param(False, "missing_directory", "No local full database backups found", id="no_local_backups"),
        pytest.param(True, "empty", None, id="empty_backup_directory"),
    ],
)
def test_get_backup_status_reports_critical_without_local_backups(
    tmp_path, monkeypatch, create_dir, state, message_part
):
    backup_dir = tmp_path / "full-backups"
    if create_dir:
        backup_dir.mkdir()
    monkeypatch.setenv("PT_EXTERNAL_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("PT_BACKUP_LOG_PATH", str(tmp_path / "missing.log"))
    monkeypatch.setenv("PT_BACKUP_LAUNCH_AGENT_PATH", str(tmp_path / "missing.plist"))
    monkeypatch.delenv("PT_BACKUP_RCLONE_DEST", raising=False)

    status = backup_reader.get_backup_status()

    assert status["status"] == "critical"
    assert status["local_full"]["state"] == state
    if message_part is not None:  # asserted only for the missing-directory case
        assert status["local_full"]["status"] == "critical"
        assert message_part in status["message"]


def test_get_backup_status_warns_when_cloud_copy_is_unconfigured(tmp_path, monkeypatch):
    backup_dir = tmp_path / "full-backups"
    log_path = tmp_path / "backup.log"
    _touch_with_age(backup_dir / "tracker_20260423_141355.db", timedelta(hours=2))
    _write(log_path, f"{_iso_age(timedelta(hours=2))} | backup | 3510272 bytes | /tmp/tracker_20260423_141355.db")

    monkeypatch.setenv("PT_EXTERNAL_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("PT_BACKUP_LOG_PATH", str(log_path))
    monkeypatch.setenv("PT_BACKUP_LAUNCH_AGENT_PATH", str(tmp_path / "missing.plist"))
    monkeypatch.delenv("PT_BACKUP_RCLONE_DEST", raising=False)

    status = backup_reader.get_backup_status()

    assert status["status"] == "warning"
    assert "safety" not in status
    assert status["cloud"]["status"] == "warning"
    assert "not configured" in status["cloud"]["message"]


def test_get_backup_status_warns_when_the_last_prune_failed(tmp_path, monkeypatch):
    backup_dir = tmp_path / "full-backups"
    log_path = tmp_path / "backup.log"
    rclone_config_path = tmp_path / "rclone.conf"
    _touch_with_age(backup_dir / "tracker_20260423_141355.db", timedelta(hours=2))
    _write(
        log_path,
        "\n".join(
            [
                f"{_iso_age(timedelta(hours=2))} | backup | ok",
                f"{_iso_age(timedelta(hours=2))} | cloud_copy | ok | b2:tracker-backups/x.db",
                f"{_iso_age(timedelta(hours=2))} | prune | failed | moved 0 file(s), 0.0 MB; failed for 2, left in place: x.db: denied",
            ]
        ),
    )
    _write(rclone_config_path, "[b2]\ntype = b2\n")
    monkeypatch.setenv("PT_EXTERNAL_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("PT_BACKUP_LOG_PATH", str(log_path))
    monkeypatch.setenv("PT_BACKUP_LAUNCH_AGENT_PATH", str(tmp_path / "missing.plist"))
    monkeypatch.setenv("PT_BACKUP_RCLONE_DEST", "b2:tracker-backups")
    monkeypatch.setenv("RCLONE_CONFIG", str(rclone_config_path))

    status = backup_reader.get_backup_status()

    assert status["status"] == "warning"
    assert "Pruning old backups failed" in status["message"] and "x.db: denied" in status["message"]

    # Not hidden behind a cloud warning either.
    monkeypatch.setenv("PT_BACKUP_RCLONE_DEST", "")
    status = backup_reader.get_backup_status()
    assert status["status"] == "warning"
    assert "not configured" in status["message"] and "Pruning old backups failed" in status["message"]


def test_the_scheduled_jobs_settings_are_read_from_its_launch_agent(tmp_path, monkeypatch):
    from scripts import backup_config

    monkeypatch.delenv("PT_BACKUP_LAUNCH_AGENT_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert backup_config.launch_agent_path() == tmp_path / "Library/LaunchAgents/local.project-tracker.backup.plist"
