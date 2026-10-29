#!/usr/bin/env python3
"""Quick test script to view loop execution status."""

import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add project-tracker to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.db.manager import DatabaseManager


def get_loop_status():
    """Get status of all autonomous loops."""
    db = DatabaseManager()
    
    loops = ["janitor", "patch-bot"]
    status_data = []
    
    executions = db.loop_last_executions(loop_names=loops)

    for loop_name in loops:
        last_run = executions.get(loop_name)

        if last_run:
            started = datetime.fromisoformat(last_run["started_at"])
            completed = (
                datetime.fromisoformat(last_run["completed_at"])
                if last_run["completed_at"]
                else None
            )
            
            # Calculate health status
            now = datetime.now()
            time_since_run = now - started
            
            # Expected intervals (in hours)
            expected_intervals = {
                "janitor": 1,      # Hourly
                "patch-bot": 0.5   # Every 30 minutes
            }
            
            expected_hours = expected_intervals.get(loop_name, 24)
            expected_delta = timedelta(hours=expected_hours)
            
            # Determine health
            if last_run["status"] == "failed":
                health = "🔴 Failed"
            elif time_since_run > expected_delta * 2:
                health = "🔴 Overdue"
            elif time_since_run > expected_delta * 1.5:
                health = "🟡 Warning"
            else:
                health = "🟢 Healthy"
            
            status_data.append({
                "loop": loop_name,
                "health": health,
                "last_run": started.strftime("%Y-%m-%d %H:%M:%S"),
                "status": last_run["status"],
                "cards_created": last_run["cards_created"],
                "duration": f"{(completed - started).total_seconds():.2f}s" if completed else "running",
                "error": last_run["error_message"] or None
            })
        else:
            status_data.append({
                "loop": loop_name,
                "health": "⚪ Never Run",
                "last_run": "N/A",
                "status": "N/A",
                "cards_created": 0,
                "duration": "N/A",
                "error": None
            })

    return status_data


if __name__ == "__main__":
    print("\n🔍 Autonomous Loop Status\n")
    print("=" * 80)
    
    status = get_loop_status()
    
    for loop in status:
        print(f"\n{loop['loop'].upper()}")
        print(f"  Health:        {loop['health']}")
        print(f"  Last Run:      {loop['last_run']}")
        print(f"  Status:        {loop['status']}")
        print(f"  Cards Created: {loop['cards_created']}")
        print(f"  Duration:      {loop['duration']}")
        if loop['error']:
            print(f"  Error:         {loop['error']}")
    
    print("\n" + "=" * 80)
    print("\n✅ All loops operational\n")
