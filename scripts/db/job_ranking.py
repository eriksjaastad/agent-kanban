"""Fit score, ranking and stale split for open job and volunteer listings.

The owner (#101679546294759424): "both the volunteer listing and job listing page
should not be stale and try and list the most interesting at the top. Having
100 listings doesn't help me that much."

The score is a plain sum, so a surprising rank can be explained by reading
the inputs below. The fit keywords are the local config's [jobs] fit_keywords
(none means no fit boost); the weights are here and nowhere else. Seniority words are deliberately absent: the owner's
2026-09-23 rule is that remote roles are never hidden or buried by title or
level. Demoted rows still show, just lower. The one exception: Staff/
Principal-only titles are left out of the Jobs list (the owner, 2026-10-15;
db.jobs.get_ranked_open_jobs).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable

TITLE_POINTS = 3          # per distinct keyword in the title
RAW_POINTS = 1            # per distinct keyword in the raw text...
RAW_POINTS_CAP = 6        # ...capped so a long posting can't outweigh its title
PAY_POINTS = 2            # a $ figure, an hourly rate or a salary is mentioned
REMOTE_US_POINTS = 2      # explicitly remote for US-based people
RECENT_POINTS = ((7, 2), (14, 1))  # (max age in days, points), first match wins
HN_FULL_POST_PENALTY = 8  # whole HN comment; the per-role rows say it better
REVIEW_PENALTY = 4        # titles flagged "[REVIEW]" by the exporter

STALE_AFTER_DAYS = 30


def _word_pattern(keyword: str) -> re.Pattern[str]:
    # Spaces and hyphens inside a keyword stay literal; the ends must not
    # touch another letter or digit, so "ai" does not match "said".
    return re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", re.IGNORECASE)


_PAY_RE = re.compile(
    r"\$\s?\d|\d\s*/\s*(?:hr|hour)\b|\bper hour\b|\bsalary\b", re.IGNORECASE
)
# A US phrase counts only next to remote wording: "Remote (US)", "Remote -
# United States", "US remote", "remotely from the US", or "remote ... US
# only/US-based" within one line. A bare "United States" (an on-site or
# hybrid location) earns nothing.
_US = r"(?:usa|us|u\.s\.a\.|u\.s\.|united states)"
_NOT_WORD_AFTER = r"(?![a-z0-9])"
_NOT_WORD_BEFORE = r"(?<![a-z0-9])"
_REMOTE_US_RE = re.compile(
    rf"\bremote(?:ly)?\W{{0,3}}(?:(?:from|in|within|across)\s+(?:the\s+)?)?{_US}{_NOT_WORD_AFTER}"
    rf"|{_NOT_WORD_BEFORE}{_US}\W{{0,3}}remote\b"
    rf"|\bremote\b[^\n]{{0,60}}?{_NOT_WORD_BEFORE}{_US}[- ](?:only|based){_NOT_WORD_AFTER}"
    rf"|{_NOT_WORD_BEFORE}{_US}[- ](?:only|based)\b[^\n]{{0,60}}?\bremote\b",
    re.IGNORECASE,
)


def listing_date(job: dict[str, Any]) -> date | None:
    """posted_date when it parses, otherwise first_seen; None when neither does."""
    for key in ("posted_date", "first_seen"):
        value = job.get(key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
    return None


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    # Each keyword counts once however often it appears. Keywords that match
    # exactly the same places count as one, so case variants such as "react"
    # and "React" never double a listing's boost.
    found = {frozenset(m.span() for m in _word_pattern(keyword).finditer(text)) for keyword in keywords}
    found.discard(frozenset())
    return len(found)


def is_hn_full_post(job: dict[str, Any]) -> bool:
    return str(job.get("url") or "").endswith("#post") or str(
        job.get("title") or ""
    ).startswith("Full HN post")


def score_listing(job: dict[str, Any], today: date, keywords: Iterable[str] = ()) -> int:
    """Return the fit score for one listing; higher is more interesting."""
    keywords = tuple(keywords)  # read twice below, so an iterator must not run dry
    title = str(job.get("title") or "")
    raw = str(job.get("raw") or "")
    everything = f"{title}\n{job.get('location') or ''}\n{raw}"

    score = TITLE_POINTS * _keyword_hits(title, keywords)
    score += min(RAW_POINTS * _keyword_hits(raw, keywords), RAW_POINTS_CAP)
    if _PAY_RE.search(everything):
        score += PAY_POINTS
    if _REMOTE_US_RE.search(everything):
        score += REMOTE_US_POINTS
    posted = listing_date(job)
    if posted is not None:
        age = (today - posted).days
        for max_age, points in RECENT_POINTS:
            if age <= max_age:
                score += points
                break
    if is_hn_full_post(job):
        score -= HN_FULL_POST_PENALTY
    if title.startswith("[REVIEW]"):
        score -= REVIEW_PENALTY
    return score


def rank_listings(
    jobs: Iterable[dict[str, Any]], today: date, keywords: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Copies of ``jobs`` with ``fit_score`` set, best first, ties newest first."""
    keywords = tuple(keywords)
    scored = [{**job, "fit_score": score_listing(job, today, keywords)} for job in jobs]
    scored.sort(key=lambda job: (
        -job["fit_score"],
        -(listing_date(job) or date.min).toordinal(),
        -int(job.get("id") or 0),
    ))
    return scored


def split_stale(
    jobs: Iterable[dict[str, Any]], today: date, max_age_days: int = STALE_AFTER_DAYS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (fresh, stale). A listing with no usable date counts as fresh."""
    cutoff = today - timedelta(days=max_age_days)
    fresh: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for job in jobs:
        posted = listing_date(job)
        (stale if posted is not None and posted < cutoff else fresh).append(job)
    return fresh, stale


def ranked_split(
    jobs: Iterable[dict[str, Any]], today: date, keywords: Iterable[str] = (),
) -> dict[str, list[dict[str, Any]]]:
    """The API shape: ranked fresh listings and ranked stale ones."""
    keywords = tuple(keywords)
    fresh, stale = split_stale(jobs, today)
    return {"fresh": rank_listings(fresh, today, keywords), "stale": rank_listings(stale, today, keywords)}
