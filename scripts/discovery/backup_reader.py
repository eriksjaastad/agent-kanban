"""Tracker backup status helpers for CLI and dashboard surfaces."""

from __future__ import annotations

from datetime import datetime, timezone
import configparser
import logging
import os
from pathlib import Path
import plistlib
from typing import Any

from scripts.backup_config import (
    external_backup_dir as _backup_root,
    launch_agent_path as _launch_agent_path,
    rclone_config_path as _rclone_config_path,
    rclone_destination as _configured_rclone_dest,
)

logger = logging.getLogger(__name__)

_LOCAL_BACKUP_HEALTHY_HOURS = 12
_LOCAL_BACKUP_STALE_HOURS = 24
_CLOUD_BACKUP_HEALTHY_HOURS = 36


def _backup_log_path() -> Path:
    # governance: allow-silent SF003: the override is optional; empty falls back to ~/.project-tracker/backup.log below
    configured = os.getenv("PT_BACKUP_LOG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".project-tracker" / "backup.log"


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:  # governance: allow-silent SF002: None means "time unknown"; backup_status treats it as no recorded success or infinitely stale (warning/critical), never healthy
        return None


def _dt_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _human_age(dt: datetime | None, now: datetime | None = None) -> str | None:
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    diff = now - dt.astimezone(timezone.utc)
    seconds = max(int(diff.total_seconds()), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    return f"{days}d ago"


def _latest_matching_file(directory: Path, pattern: str) -> dict[str, Any]:
    if not directory.exists():
        logger.warning("Backup directory does not exist: %s", directory)
        return {
            "exists": False,
            "count": 0,
            "path": None,
            "timestamp": None,
            "size_bytes": None,
            "age_human": None,
            "state": "missing_directory",
            "error": None,
        }

    try:
        files = [path for path in directory.glob(pattern) if path.is_file()]
    except OSError as err:
        logger.warning("Failed to scan backup directory %s: %s", directory, err)
        return {
            "exists": False,
            "count": 0,
            "path": None,
            "timestamp": None,
            "size_bytes": None,
            "age_human": None,
            "state": "scan_error",
            "error": str(err),
        }

    if not files:
        logger.warning("No backup files matching %s found in %s", pattern, directory)
        return {
            "exists": False,
            "count": 0,
            "path": None,
            "timestamp": None,
            "size_bytes": None,
            "age_human": None,
            "state": "empty",
            "error": None,
        }

    try:
        latest = max(files, key=lambda path: path.stat().st_mtime)
        latest_stat = latest.stat()
    except OSError as err:
        logger.warning("Failed to read backup file metadata in %s: %s", directory, err)
        return {
            "exists": False,
            "count": len(files),
            "path": None,
            "timestamp": None,
            "size_bytes": None,
            "age_human": None,
            "state": "metadata_error",
            "error": str(err),
        }

    ts = datetime.fromtimestamp(latest_stat.st_mtime, tz=timezone.utc)
    return {
        "exists": True,
        "count": len(files),
        "path": str(latest),
        "timestamp": _dt_to_iso(ts),
        "size_bytes": latest_stat.st_size,
        "age_human": _human_age(ts),
        "state": "found",
        "error": None,
    }


def _parse_rclone_config() -> dict[str, dict[str, str]]:
    config_path = _rclone_config_path()
    if not config_path.exists():
        return {}

    config = configparser.ConfigParser()
    try:
        config.read(config_path)
    except Exception as err:  # governance: allow-silent SF002: no readable remotes makes the configured remote count as unavailable, so backup_status reports a cloud warning, never healthy
        logger.warning("Failed to read rclone config at %s: %s", config_path, err)
        return {}

    remotes: dict[str, dict[str, str]] = {}
    for section in config.sections():
        remotes[section] = dict(config[section])
    return remotes


def _parse_launch_agent() -> dict[str, Any]:
    path = _launch_agent_path()
    info: dict[str, Any] = {
        "present": path.exists(),
        "path": str(path),
        "label": None,
        "start_interval_seconds": None,
        "schedule": None,
    }
    if not path.exists():
        return info

    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except Exception as err:
        logger.warning("Failed to parse backup LaunchAgent at %s: %s", path, err)
        return info

    interval = payload.get("StartInterval")
    schedule = None
    if isinstance(interval, int) and interval > 0:
        hours = interval // 3600
        schedule = f"Every {hours}h" if hours else f"Every {interval}s"

    info.update(
        {
            "label": payload.get("Label"),
            "start_interval_seconds": interval,
            "schedule": schedule,
        }
    )
    return info


def append_cloud_copy_log(*, ok: bool, detail: str = "") -> Path:
    """Append a cloud_copy line so `pt backup status` sees manual offsite runs."""
    log_path = _backup_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    outcome = "ok" if ok else "failed"
    # Keep detail to one line; strip CR / newlines from CLI output.
    clean = " ".join(detail.replace(chr(13), " ").split())
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} | cloud_copy | {outcome}"
    if clean:
        line = f"{line} | {clean}"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return log_path


def _parse_backup_log() -> dict[str, Any]:
    log_path = _backup_log_path()
    summary: dict[str, Any] = {
        "exists": log_path.exists(),
        "path": str(log_path),
        "last_local_success_at": None,
        "last_cloud_success_at": None,
        "last_cloud_failure_at": None,
        "last_cloud_failure_detail": None,
        "last_prune_failed": False,
        "last_prune_at": None,
        "last_prune_detail": None,
    }
    if not log_path.exists():
        return summary

    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except Exception as err:
        logger.warning("Failed to read backup log at %s: %s", log_path, err)
        return summary

    for line in lines:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            continue
        ts = _parse_iso(parts[0])
        if ts is None:
            continue

        kind = parts[1]
        if kind == "backup":
            summary["last_local_success_at"] = _dt_to_iso(ts)
            continue

        if kind == "prune" and len(parts) >= 3:
            summary["last_prune_at"] = _dt_to_iso(ts)
            summary["last_prune_failed"] = parts[2] == "failed"
            summary["last_prune_detail"] = (parts[3] if len(parts) > 3 else "") or None
            continue

        if kind != "cloud_copy" or len(parts) < 3:
            continue

        outcome = parts[2]
        # Accept both historical "success"/"failure" and #7231 "ok"/"failed".
        if outcome in ("success", "ok"):
            summary["last_cloud_success_at"] = _dt_to_iso(ts)
        elif outcome in ("failure", "failed"):
            summary["last_cloud_failure_at"] = _dt_to_iso(ts)
            detail = parts[3] if len(parts) > 3 else ""
            summary["last_cloud_failure_detail"] = detail or None

    return summary


def _severity_color(status: str) -> str:
    return {
        "healthy": "green",
        "warning": "yellow",
        "critical": "red",
    }.get(status, "blue")


def _remote_name(dest: str) -> str | None:
    if ":" not in dest:
        return None
    return dest.split(":", 1)[0]


def get_backup_status() -> dict[str, Any]:
    """Build the shared backup status payload for CLI and dashboard use."""
    now = datetime.now(timezone.utc)
    backup_dir = _backup_root()
    local = _latest_matching_file(backup_dir, "tracker_*.db")
    log = _parse_backup_log()
    launch_agent = _parse_launch_agent()
    remotes = _parse_rclone_config()
    configured_dest = _configured_rclone_dest()
    configured_remote = _remote_name(configured_dest)
    cloud_success_at = _parse_iso(log["last_cloud_success_at"])

    local_at = _parse_iso(local["timestamp"])
    if not local["exists"]:
        local_status = "critical"
        local_message = "No local full database backups found."
    else:
        local_age_hours = (now - local_at).total_seconds() / 3600 if local_at else float("inf")
        if local_age_hours <= _LOCAL_BACKUP_HEALTHY_HOURS:
            local_status = "healthy"
            local_message = f"Latest full backup {local['age_human']}."
        elif local_age_hours <= _LOCAL_BACKUP_STALE_HOURS:
            local_status = "warning"
            local_message = f"Latest full backup is getting old ({local['age_human']})."
        else:
            local_status = "critical"
            local_message = f"Latest full backup is stale ({local['age_human']})."

    if not configured_dest:
        cloud_status = "warning"
        cloud_message = "Off-machine copy is not configured."
    elif configured_remote and configured_remote not in remotes:
        cloud_status = "warning"
        cloud_message = f"Configured rclone remote '{configured_remote}' is not available."
    elif cloud_success_at is None:
        cloud_status = "warning"
        cloud_message = "No successful off-machine copy recorded yet."
    else:
        cloud_age_hours = (now - cloud_success_at).total_seconds() / 3600
        if cloud_age_hours <= _CLOUD_BACKUP_HEALTHY_HOURS:
            cloud_status = "healthy"
            cloud_message = f"Latest off-machine copy {_human_age(cloud_success_at, now)}."
        else:
            cloud_status = "warning"
            cloud_message = f"Off-machine copy is stale ({_human_age(cloud_success_at, now)})."

    if local_status == "critical":
        status = "critical"
        message = local_message
    elif cloud_status != "healthy":
        status = "warning"
        message = f"{local_message} {cloud_message}"
    else:
        status = "healthy"
        message = "Local and off-machine backups look healthy."
    # A failed prune is reported whatever else is wrong, never hidden behind it.
    if log["last_prune_failed"]:
        prune_message = f"Pruning old backups failed: {log['last_prune_detail']}"
        if status == "healthy":
            status, message = "warning", prune_message
        else:
            message = f"{message} {prune_message}"

    last_local_log = _parse_iso(log["last_local_success_at"])
    return {
        "status": status,
        "status_color": _severity_color(status),
        "message": message,
        "backup_dir": str(backup_dir),
        "local_full": {
            **local,
            "status": local_status,
            "message": local_message,
        },
        "cloud": {
            "status": cloud_status,
            "message": cloud_message,
            "configured": bool(configured_dest),
            "configured_dest": configured_dest or None,
            "configured_remote": configured_remote,
            "last_success_at": _dt_to_iso(cloud_success_at),
            "last_success_age_human": _human_age(cloud_success_at, now),
        },
        "launch_agent": launch_agent,
        "log": {
            **log,
            "last_local_success_age_human": _human_age(last_local_log, now),
            "last_cloud_success_age_human": _human_age(cloud_success_at, now),
        },
        "remotes": list(remotes.keys()),
        "remote_count": len(remotes),
    }
