"""Alert detection for project tracker."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any

from .cron_monitor import check_cron_health

# Add parent directory to path for logger import
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.logger import get_logger

logger = get_logger(__name__)


def detect_stalled_projects(projects: List[Dict[str, Any]], days_threshold: int = 60) -> List[Dict[str, Any]]:
    """Detect projects with no work in X days (default 60 for less noise)."""
    alerts = []
    
    for project in projects:
        if project.get("last_modified"):
            try:
                last_mod = datetime.fromisoformat(project["last_modified"].replace('Z', '+00:00'))
                # Use timezone-aware now if last_mod has timezone info
                now = datetime.now(last_mod.tzinfo) if last_mod.tzinfo else datetime.now()
                cutoff_date = now - timedelta(days=days_threshold)
                
                if last_mod < cutoff_date:
                    days_ago = (now - last_mod).days
                    alerts.append({
                        "project_id": project["id"],
                        "project_name": project["name"],
                        "type": "stalled",
                        "severity": "warning",
                        "message": f"No work in {days_ago} days",
                        "details": f"Last modified: {project['last_modified'].split('T')[0]}"
                    })
            except Exception as e:
                logger.debug(f"Failed to parse last_modified for {project.get('name', 'unknown')}: {e}")
    
    return alerts


def detect_cron_failures(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect cron job failures and issues.

    Expects projects pre-enriched with ``cron_jobs`` key via _bulk_enrich.
    """
    alerts = []
    if projects and "cron_jobs" not in projects[0]:
        logger.warning("detect_cron_failures: projects missing cron_jobs key — was _bulk_enrich called?")
        return alerts

    for project in projects:
        cron_jobs = project.get("cron_jobs", [])
        if not cron_jobs:
            continue
        
        # Check each cron job's health
        issues = check_cron_health(
            project["id"],
            cron_jobs,
            project["path"]
        )
        
        for issue in issues:
            severity = "critical" if issue["type"] in ["execution_error", "missed_run"] else "warning"
            
            alerts.append({
                "project_id": project["id"],
                "project_name": project["name"],
                "type": f"cron_{issue['type']}",
                "severity": severity,
                "message": issue["message"],
                "details": f"{issue.get('description', issue.get('command', ''))}"
            })
    
    return alerts


def detect_missing_index(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect projects missing index files.

    Note: 00_Index files were removed (Librarian system deleted).
    This function now returns an empty list — index file checks
    are no longer applicable.
    """
    return []


def detect_invalid_frontmatter(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect projects with invalid frontmatter. Disabled; returns [].

    Note: 00_Index files were removed (Librarian system deleted).
    This function now returns an empty list — frontmatter validation
    is no longer applicable without index files.
    """
    return []


def get_all_alerts(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Get all alerts for all projects."""
    all_alerts = []
    
    # Detect different types of issues
    all_alerts.extend(detect_cron_failures(projects))
    all_alerts.extend(detect_stalled_projects(projects))
    all_alerts.extend(detect_missing_index(projects))  # Only missing, not incomplete
    # Sort by severity (critical first, then warning, then info)
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    all_alerts.sort(key=lambda x: (severity_order.get(x["severity"], 3), x["project_name"]))
    
    return all_alerts
