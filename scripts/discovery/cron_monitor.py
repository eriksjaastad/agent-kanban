"""Cron job monitoring and failure detection."""

import sys
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from croniter import croniter
except ModuleNotFoundError:  # pragma: no cover - depends on optional environment dependency
    croniter = None

# Add parent directory to path for logger import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.logger import get_logger

logger = get_logger(__name__)


def check_cron_health(project_id: str, cron_jobs: List[Dict[str, str]], project_path: str) -> List[Dict[str, str]]:
    """
    Check health of cron jobs for a project.

    Returns list of issues found (empty if all healthy).

    Note: Only alerts for ACTUAL problems (execution errors, missed runs).
    Does NOT alert for:
    - Template placeholders ({{...}}) - these are unfilled scaffolding
    - Jobs not in crontab - these might just be dormant/planned
    """
    issues = []

    for job in cron_jobs:
        schedule = job.get("schedule", "")
        command = job.get("command", "")
        description = job.get("description", "")

        if not schedule or not command:
            continue

        # Skip template placeholders - these are scaffolding, not real configs
        if "{{" in schedule or "}}" in schedule:
            continue

        # Check if cron expression is valid (only for non-template schedules)
        if not is_valid_cron(schedule):
            issues.append({
                "type": "invalid_schedule",
                "schedule": schedule,
                "command": command,
                "description": description,
                "message": f"Invalid cron schedule: {schedule}"
            })
            continue

        # Skip "not installed" check - too noisy for dormant/planned crons
        # Only check for execution issues on crons we can verify are running
        is_installed, crontab_entry = check_crontab_installed(command)
        if not is_installed:
            # Don't alert - might just be planned/dormant
            continue
        
        # Check for log file
        log_file = find_log_file(project_path, command)
        if log_file:
            last_run, status = check_log_file(log_file)
            
            if last_run:
                expected_run = get_expected_next_run(schedule, last_run)
                now = datetime.now()
                
                # If we're past the expected next run by more than 1 hour
                if expected_run and now > expected_run + timedelta(hours=1):
                    issues.append({
                        "type": "missed_run",
                        "schedule": schedule,
                        "command": command,
                        "description": description,
                        "last_run": last_run.isoformat() if last_run else None,
                        "expected_run": expected_run.isoformat(),
                        "message": f"Job hasn't run since {format_time_ago(last_run)} (expected: {schedule})"
                    })
                
                # Check if last run had errors
                if status == "error":
                    issues.append({
                        "type": "execution_error",
                        "schedule": schedule,
                        "command": command,
                        "description": description,
                        "last_run": last_run.isoformat() if last_run else None,
                        "message": f"Last execution failed ({format_time_ago(last_run)})"
                    })
    
    return issues


def is_valid_cron(schedule: str) -> bool:
    """Check if cron schedule is valid."""
    try:
        # Handle special cases
        if schedule.startswith("@"):
            return schedule in ["@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly", "@reboot"]

        if croniter is None:
            logger.warning("croniter is not installed; skipping standard cron validation")
            return False
        
        # Validate standard cron expression
        croniter(schedule)
        return True
    except ValueError as e:  # governance: allow-silent SF002: croniter rejects a malformed expression with CroniterError (a ValueError); False is the true answer and check_cron_health reports it as an invalid_schedule issue
        logger.debug(f"Invalid cron schedule '{schedule}': {e}")
        return False


def check_crontab_installed(command: str) -> Tuple[bool, Optional[str]]:
    """Check if command is in user's crontab."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode != 0:
            return False, None
        
        crontab = result.stdout
        
        # Look for command in crontab (may be wrapped in different ways)
        for line in crontab.split('\n'):
            if line.strip().startswith('#'):
                continue
            if command in line or Path(command).name in line:
                return True, line.strip()
        
        return False, None
    except Exception as e:
        logger.warning(f"Failed to check crontab for command '{command}': {e}")
        return False, None


def find_log_file(project_path: str, command: str) -> Optional[Path]:
    """Try to find log file for the cron job."""
    project_dir = Path(project_path)
    
    # Common log locations
    log_patterns = [
        "logs/cron.log",
        "logs/*.log",
        "cron.log",
        "*.log"
    ]
    
    for pattern in log_patterns:
        log_files = list(project_dir.glob(pattern))
        if log_files:
            # Return the most recently modified log file
            return max(log_files, key=lambda p: p.stat().st_mtime)
    
    return None


def check_log_file(log_path: Path) -> Tuple[Optional[datetime], Optional[str]]:
    """
    Check log file for last run and status.
    
    Returns (last_run_time, status) where status is 'success' or 'error'
    """
    try:
        content = log_path.read_text()
        lines = content.split('\n')
        
        # Look for timestamp patterns and error indicators
        last_timestamp = None
        last_status = "success"
        
        # Common timestamp patterns
        timestamp_patterns = [
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}',  # ISO format
            r'\w{3} \w{3} \d{1,2} \d{2}:\d{2}:\d{2} \d{4}',  # Cron format
        ]
        
        # Error patterns
        error_patterns = [
            r'error',
            r'failed',
            r'exception',
            r'traceback',
            r'fatal',
        ]
        
        # Parse from end of file (most recent)
        for line in reversed(lines[-100:]):  # Check last 100 lines
            # Check for errors
            if not last_timestamp or last_status == "success":
                for pattern in error_patterns:
                    if re.search(pattern, line, re.IGNORECASE):
                        last_status = "error"
                        break
            
            # Look for timestamp
            if not last_timestamp:
                for pattern in timestamp_patterns:
                    match = re.search(pattern, line)
                    if match:
                        try:
                            timestamp_str = match.group(0)
                            # Try to parse it
                            last_timestamp = parse_timestamp(timestamp_str)
                            break
                        except Exception as e:
                            logger.debug(f"Failed to parse timestamp '{timestamp_str}': {e}")
                            continue
            
            if last_timestamp:
                break
        
        return last_timestamp, last_status
    except Exception as e:
        logger.warning(f"Failed to check log file {log_path}: {e}")
        return None, None


def parse_timestamp(timestamp_str: str) -> datetime:
    """Parse various timestamp formats."""
    # Try ISO format first
    try:
        return datetime.fromisoformat(timestamp_str.replace(' ', 'T'))
    except Exception as e:
        logger.debug(f"Failed to parse ISO timestamp '{timestamp_str}': {e}")
    
    # Try other common formats
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%a %b %d %H:%M:%S %Y",
        "%Y-%m-%dT%H:%M:%S",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(timestamp_str, fmt)
        except Exception as e:
            logger.debug(f"Failed to parse timestamp '{timestamp_str}' with format '{fmt}': {e}")
            continue
    
    raise ValueError(f"Could not parse timestamp: {timestamp_str}")


def get_expected_next_run(schedule: str, last_run: datetime) -> Optional[datetime]:
    """Calculate when the job should have run next after last_run."""
    try:
        if schedule.startswith("@"):
            # Special schedules
            intervals = {
                "@hourly": timedelta(hours=1),
                "@daily": timedelta(days=1),
                "@midnight": timedelta(days=1),
                "@weekly": timedelta(weeks=1),
                "@monthly": timedelta(days=30),
                "@yearly": timedelta(days=365),
                "@annually": timedelta(days=365),
            }
            interval = intervals.get(schedule)
            if interval:
                return last_run + interval

        if croniter is None:
            logger.warning("croniter is not installed; cannot calculate next standard cron run")
            return None
        
        # Standard cron expression
        cron = croniter(schedule, last_run)
        return cron.get_next(datetime)
    except ValueError as e:  # governance: allow-silent SF002: check_cron_health validates the schedule first, so a CroniterError here means croniter cannot step this schedule; None skips only the missed-run check and is logged at WARNING, other errors raise
        logger.warning(f"Failed to calculate next run for schedule '{schedule}': {e}")
        return None


def format_time_ago(dt: datetime) -> str:
    """Format datetime as 'X days ago' or 'X hours ago'."""
    if not dt:
        return "unknown"
    
    now = datetime.now()
    diff = now - dt
    
    if diff.days > 0:
        return f"{diff.days} day{'s' if diff.days != 1 else ''} ago"
    elif diff.seconds >= 3600:
        hours = diff.seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif diff.seconds >= 60:
        minutes = diff.seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    else:
        return "just now"

