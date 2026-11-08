"""User-visible changes per live site per week (#8845): the public-changelog test.

A live site is a monitoring entry in EXTERNAL_RESOURCES.yaml whose prod_url
answers HTTP 200 now. A user-visible change is a non-merge commit on that
project's main whose subject is typed feat or fix; docs, chore, test and the
rest are not. Vercel deploys here are manual, so reaching main stands in for
reaching production.
"""

import re
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

import yaml
from fastapi import APIRouter

from dashboard.github_cache import GitHubCache
from scripts.config import EXTERNAL_RESOURCES_FILE
from scripts.config import projects_root as config_projects_root

router = APIRouter()

_USER_VISIBLE = re.compile(r"(feat|fix)(\([^)]*\))?!?:")
_cache: Dict = {"key": None, "cache": None}


def _status(url: str) -> int | str:
    """One GET: the HTTP status, or the error's name when there is none."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pt-dashboard"}),
                                    timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        return type(exc).__name__


def _checkout(key: str, entry: dict) -> Path:
    """The project's checkout: named by its repo URL when there is one, else by the registry key."""
    repo = str(entry.get("repo") or "")
    name = repo.rstrip("/").split("/")[-1].removesuffix(".git") if repo.startswith("https://") else key
    return config_projects_root() / name


def _changes(checkout: Path, starts: List[date]) -> List[int]:
    """feat/fix commits per week on main; a git failure raises so the site is not shown as zero."""
    since = datetime.combine(starts[0], datetime.min.time()).astimezone().isoformat()
    log = subprocess.run(["git", "-C", str(checkout), "log", "main", "--no-merges", f"--since={since}",
                          "--format=%cI%x09%s"], capture_output=True, text=True, timeout=30, check=True).stdout
    counts = {s: 0 for s in starts}
    for line in log.splitlines():
        when, subject = line.split("\t", 1)
        day = datetime.fromisoformat(when).astimezone().date()
        week = day - timedelta(days=day.weekday())
        if week in counts and _USER_VISIBLE.match(subject):
            counts[week] += 1
    return [counts[s] for s in starts]


def _fetch(starts: List[date]) -> Dict:
    monitoring = yaml.safe_load(EXTERNAL_RESOURCES_FILE.read_text())["monitoring"]
    entries = {k: v for k, v in monitoring.items()
               if not k.startswith("_") and str(v.get("prod_url") or "").startswith("http")}
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = dict(zip(entries, pool.map(lambda k: _status(entries[k]["prod_url"]), entries)))
    sites, not_live = [], []
    for key, entry in entries.items():
        if statuses[key] != 200:
            not_live.append({"project": key, "url": entry["prod_url"], "status": statuses[key]})
            continue
        checkout = _checkout(key, entry)
        # A site with no checkout here (logicdesigns) is listed, its changes unknown rather than zero.
        changes = _changes(checkout, starts) if (checkout / ".git").exists() else None
        sites.append({"project": key, "url": entry["prod_url"], "changes": changes})
    return {"week_starts": [s.isoformat() for s in starts], "sites": sites, "not_live": not_live}


@router.get("/api/stats/live-site-changes")
def live_site_changes():
    """Per live site, user-visible changes in each of the last 8 weeks (Monday start, this machine's
    clock), zeros included. Collected in the background like /api/github; a failure is refresh_error."""
    this_monday = date.today() - timedelta(days=date.today().weekday())
    starts = [this_monday - timedelta(weeks=n) for n in range(7, -1, -1)]
    if _cache["key"] != starts:
        _cache["key"], _cache["cache"] = starts, GitHubCache(ttl=900, label="Live sites")
    return _cache["cache"].read(lambda: _fetch(starts))
