"""Merged PRs per week, user-facing repos vs the rest (#8844).

gh is stubbed: the search gives each week's total, and each user-facing repo
lists its own merges, bucketed into weeks on this machine's clock.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dashboard.routes import github

MONDAY = date(2026, 10, 26)
STARTS = [MONDAY - timedelta(weeks=1), MONDAY]


def _utc(day: date, hour: int) -> str:
    """A local wall-clock time on ``day``, written as GitHub writes it (UTC, Z)."""
    local = datetime.combine(day, datetime.min.time()).replace(hour=hour).astimezone()
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stub(monkeypatch, totals, repos):
    """totals: one total per week, or None for a failed search. repos: name -> (prs, failure)."""
    searches = []

    def fake(args, timeout=30, missing_ok=False):
        if args[:2] == ["api", "/user"]:
            return {"login": "owner"}, github.GH_OK
        if "search/issues" in args:
            searches.append(args[-1])
            total = totals[len(searches) - 1]
            return total, (github.GH_OK if total is not None else github.GH_ERROR)
        return repos[args[3].split("/")[1]]

    monkeypatch.setattr(github, "_gh_call", fake)
    return searches


def test_user_facing_merges_are_split_out_of_each_weeks_total(monkeypatch):
    prs = [
        {"mergedAt": _utc(MONDAY, 0)},                         # first hour of this week, local time
        {"mergedAt": _utc(MONDAY - timedelta(days=1), 23)},    # last hour of last week
        {"mergedAt": _utc(MONDAY - timedelta(weeks=3), 12)},   # before the window
    ]
    searches = _stub(monkeypatch, [10, 4], {
        "site": (prs, github.GH_OK),
        "typo": (None, github.GH_NOT_FOUND),
    })

    result = github._fetch_merged_weekly(frozenset({"site", "typo"}), STARTS)

    assert result == {
        "weeks": [
            {"week_start": "2026-10-19", "user_facing": 1, "internal": 9},
            {"week_start": "2026-10-26", "user_facing": 1, "internal": 3},
        ],
        "not_on_github": ["typo"],
    }
    start = datetime.combine(MONDAY, datetime.min.time()).astimezone()
    assert searches[1].startswith(f"q=user:owner is:pr is:merged merged:{start.isoformat()}..")


@pytest.mark.parametrize("totals, repos", [
    ([10, None], {}),                                                    # a week's search failed
    ([10, 4], {"site": (None, github.GH_ERROR)}),                        # a repo's list failed
    ([10, 4], {"site": ([{"mergedAt": _utc(MONDAY, 9)}] * 1000, github.GH_OK)}),  # cut off in the window
])
def test_an_incomplete_count_raises_instead_of_undercounting(monkeypatch, totals, repos):
    _stub(monkeypatch, totals, repos)
    with pytest.raises(ValueError):
        github._fetch_merged_weekly(frozenset(repos), STARTS)


def test_a_new_list_replaces_the_cached_count(monkeypatch):
    made = []

    class FakeCache:
        def __init__(self, **kwargs):
            made.append(self)

        def read(self, fetch):
            return {"cache": made.index(self)}

    monkeypatch.setattr(github, "GitHubCache", FakeCache)
    monkeypatch.setattr(github, "_merged", {"key": None, "cache": None})

    assert github.merged_prs_weekly(frozenset({"a"}), STARTS) == {"cache": 0}
    assert github.merged_prs_weekly(frozenset({"a"}), STARTS) == {"cache": 0}
    assert github.merged_prs_weekly(frozenset(), STARTS) == {"cache": 1}
