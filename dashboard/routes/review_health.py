"""Review health per week across the portfolio (#8848).

Reads each merged PR body's Review section (the pr skill writes it), or the
whole body when there is none (older per-file Record bodies). Each head named
in a verdict (`PASS on|at <sha>`, `PASS on HEAD <sha>`, `FAIL <sha>`) is one review
round (two reviewers of one head are one round), a FAIL verdict marks a failed
round, and each `- Called overthinking:` line is a finding the implementer
judged not a must-fix (ARC #8835). A review line saying PASS with no head is
"unpinned", counted apart. Findings themselves are not counted: the record
keeps the verdict per round, not each finding.
"""

import re
from datetime import date, datetime, timedelta
from statistics import median
from typing import Dict, List

from fastapi import APIRouter

from dashboard.github_cache import GitHubCache
from dashboard.routes.github import GH_OK, _gh_call, _gh_json

router = APIRouter()

_SECTION = re.compile(r"^## Review[ \t]*\r?\n(.*?)(?=^## |\Z)", re.M | re.S)
_VERDICT = re.compile(r"\b(PASS|FAIL|INCOMPLETE) (?:on |at )?(?:HEAD )?`?([0-9a-f]{7,40})\b")
_UNPINNED = re.compile(r"^(?=.*\b(?i:review|independent)\b).*\bPASS\b", re.M)
_ROUND = re.compile(r"\bround (\d+)\b", re.I)  # "PASS on <sha> (round 2)" names the earlier rounds
_OVERTHINKING = re.compile(r"^- Called overthinking:", re.M)
_cache: Dict = {"key": None, "cache": None}
_past: Dict[date, Dict] = {}


def _review_text(body: str | None) -> str:
    match = _SECTION.search(body or "")
    return match.group(1) if match else body or ""


def review_record(body: str | None) -> Dict | None:
    """Rounds, failed rounds and overthinking calls, or None when no review names a head."""
    section = _review_text(body)
    verdicts = _VERDICT.findall(section)
    if not verdicts:
        return None
    heads = {sha[:7] for _, sha in verdicts}
    return {"rounds": max([len(heads), *map(int, _ROUND.findall(section))]),
            "fails": len({sha[:7] for verdict, sha in verdicts if verdict == "FAIL"}),
            "overthinking": len(_OVERTHINKING.findall(section))}


def _week(start: date, bodies: List) -> Dict:
    records = [r for r in map(review_record, bodies) if r]
    unpinned = sum(1 for b in bodies if not review_record(b) and _UNPINNED.search(_review_text(b)))
    return {"week_start": start.isoformat(), "merged": len(bodies), "reviewed": len(records),
            "unpinned": unpinned,
            "median_rounds": median(r["rounds"] for r in records) if records else None,
            "with_fail": sum(1 for r in records if r["fails"]),
            "overthinking": sum(r["overthinking"] for r in records)}


def _search_week(owner: str, start: date) -> Dict:
    # Each boundary gets its own UTC offset, so a DST change lands on the right day.
    first, end = (datetime.combine(d, datetime.min.time()).astimezone()
                  for d in (start, start + timedelta(weeks=1)))
    prs, failure = _gh_call([
        "search", "prs", "--owner", owner, "--merged", "--json", "body", "--limit", "1000",
        "--merged-at", f"{first.isoformat()}..{(end - timedelta(seconds=1)).isoformat()}",
    ], timeout=60)
    if failure is not GH_OK or len(prs or []) >= 1000:
        raise ValueError(f"merged PR search for the week of {start} failed or hit the 1000 limit")
    return _week(start, [pr["body"] for pr in prs or []])


def _fetch(starts: List[date]) -> Dict:
    """One search a week, in turn: bodies come only from search, whose limit is 30
    requests a minute. Past weeks never change, so each is fetched once and kept;
    a refresh stopped by the limit raises and the next one resumes from there."""
    user = _gh_json(["api", "/user"])
    if user is None:
        raise ValueError("gh CLI not available or not authenticated")
    weeks = []
    for start in starts:
        week = _past.get(start) or _search_week(user["login"], start)
        if start != starts[-1]:
            _past[start] = week
        weeks.append(week)
    return {"weeks": weeks}


@router.get("/api/stats/review-health")
def review_health():
    """Per week (Monday start, this machine's clock) for the last 8 weeks: merged PRs, how many
    recorded a review, median rounds, PRs with a FAIL round, overthinking calls. Collected in the
    background like /api/github; a failure is refresh_error."""
    this_monday = date.today() - timedelta(days=date.today().weekday())
    starts = [this_monday - timedelta(weeks=n) for n in range(7, -1, -1)]
    if _cache["key"] != starts:
        _cache["key"], _cache["cache"] = starts, GitHubCache(ttl=900, label="Review health")
    return _cache["cache"].read(lambda: _fetch(starts))
