#!/usr/bin/env python3
"""Portable project-activity scanner — emits one machine's status as JSON.

Designed to run on ANY machine (esp. the Mac Mini) with only the standard
library, so it can be piped over SSH (`ssh host python3 - < mini_scan.py`) with
no deployment step, or scheduled locally to push a snapshot later.

Activity is measured by newest file mtime, NOT git — several real projects on
the Mini (e.g. agent-improvement-research) aren't under version control, and a
git-only scan would wrongly report them as dead. The digest turns this JSON into
the "Mac Mini" section of the daily email.

Output (stdout): JSON
  {
    "machine": "<hostname>",
    "scanned_at": "<ISO8601 UTC>",
    "projects": [{"name": ..., "days_since": int, "last_activity": "YYYY-MM-DD"}],
    "error": null
  }
projects is sorted most-recent-first.
"""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# launchd label prefixes to report on: alert_digest.py passes the [digest]
# job_label_prefixes from its local config as arguments; none means no jobs.
JOB_LABEL_PREFIXES = tuple(sys.argv[1:])

PROJECTS_DIR = Path(os.environ.get("PT_PROJECTS_DIR", str(Path.home() / "projects")))

# Directories whose mtimes should never count as "project activity".
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "logs", ".DS_Store", "dist", "build",
    ".next", ".turbo", "target", ".cache", "artifacts",
}
# Files that get rewritten by tooling and would mask true activity.
SKIP_FILE_SUFFIXES = (".log", ".jsonl", ".pyc", ".lock")


def newest_mtime(root: Path) -> float:
    """Return the newest mtime under root, skipping junk dirs/files. 0.0 if none."""
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune junk dirs in place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith(".") or fn.endswith(SKIP_FILE_SUFFIXES):
                continue
            try:
                m = (Path(dirpath) / fn).stat().st_mtime
                if m > newest:
                    newest = m
            except OSError:
                continue
    return newest


def collect_jobs():
    """Our launchd jobs on this machine via `launchctl list`.

    Returns a list (possibly empty = genuinely no matching jobs), or None if
    launchctl could not be run — so the reader can distinguish "no jobs" from
    "couldn't read jobs" instead of being misled by an empty list.
    """
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=15
        )
        if proc.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):  # governance: allow-silent SF002: None is the documented "couldn't read jobs" value, distinct from an empty list
        return None
    jobs = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid_s, status_s, label = parts
        if not label.startswith(JOB_LABEL_PREFIXES):
            continue
        try:
            last_exit = None if status_s == "-" else int(status_s)
        except ValueError:
            last_exit = None
        jobs.append({"label": label, "pid": None if pid_s == "-" else pid_s, "last_exit": last_exit})
    return jobs


def scan() -> dict:
    now = time.time()
    projects = []
    if PROJECTS_DIR.is_dir():
        for child in sorted(PROJECTS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            m = newest_mtime(child)
            if m == 0.0:
                continue
            days = int((now - m) // 86400)
            projects.append(
                {
                    "name": child.name,
                    "days_since": days,
                    "last_activity": datetime.fromtimestamp(m, timezone.utc).strftime("%Y-%m-%d"),
                }
            )
    projects.sort(key=lambda p: p["days_since"])
    return {
        "machine": socket.gethostname(),
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "projects": projects,
        "jobs": collect_jobs(),
        "error": None,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(scan()))
    except Exception as exc:  # noqa: BLE001 — must always emit parseable JSON
        print(json.dumps({"machine": socket.gethostname(), "projects": [], "error": str(exc)}))
        sys.exit(1)
