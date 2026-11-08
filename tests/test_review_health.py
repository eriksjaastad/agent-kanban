"""Review health per week (#8848): rounds, FAIL rounds, overthinking calls and unpinned reviews from PR bodies.

Bodies follow the shapes merged PRs actually carry; gh is stubbed, so no request leaves the machine.
"""

from datetime import date, timedelta

import pytest

from dashboard.routes import review_health
from dashboard.routes.github import GH_ERROR, GH_OK

A, B, C, D = "5405187" + "a" * 33, "d608c9c" + "b" * 33, "c481b80" + "c" * 33, "9e0f1a2" + "d" * 33


def _body(review: str) -> str:
    # Verdicts in other sections are prose, not review rounds.
    return (f"## Summary\n- PASS on {D} in prose is not a review\n\n## Review\n{review}\n"
            f"## Related\n- #1, which reverted the FAIL on {D[:7]} commit\n")


@pytest.mark.parametrize("review, expected", [
    # Two reviewers of one head, short and full SHA, are one round.
    (f"- Claude preflight: PASS on {A[:7]}\n- Codex: PASS on {A}\n", (1, 0, 0)),
    (f"- Reviewer subagent: FAIL on {A[:7]} (must-fix, fixed), INCOMPLETE on {B[:7]}, PASS on {B}\n"
     f"- Called overthinking: a 3 GB body — no caller sends one\n"
     f"- Called overthinking: NUL in a branch name — git refuses it\n", (2, 1, 2)),
    (f"- Codex: FAIL on {A[:7]}, FAIL on {B[:7]}, PASS on {C}.\n", (3, 2, 0)),
    (f"- Codex: PASS on {A} (round 3; rounds 1-2 found a typo)\n", (3, 0, 0)),
    # Other shapes merged PRs carry.
    (f"- Independent review: PASS on HEAD `{A}`\n", (1, 0, 0)),
    (f"- Local and independent full-diff reviews PASS at {A}.\n", (1, 0, 0)),
    (f"- Codex: FAIL {A[:7]}, PASS {B}\n- exact-HEAD review: PASS `{B}`\n", (2, 1, 0)),
])
def test_rounds_fails_and_overthinking_come_from_the_review_section(review, expected):
    record = review_health.review_record(_body(review))
    assert (record["rounds"], record["fails"], record["overthinking"]) == expected


def test_a_body_without_a_review_section_is_read_whole():
    record = review_health.review_record(
        f"# Record at {A[:7]}\nSelf-review at {A}: caught nothing.\n"
        f"Round 2 (after FAIL {A[:7]}: a stale cursor):\nCodex: PASS {B}\n")
    assert (record["rounds"], record["fails"]) == (2, 1)


@pytest.mark.parametrize("body", [
    None, "", "## Summary\n- docs only\n",
    "## Review\nLocal HEAD review: **PASS** (no blocking findings).\n",  # no verdict on a head
    "## Verification\n- independent exact-head review: PASS with no findings\n",
])
def test_a_pr_whose_review_names_no_head_has_no_record(body):
    assert review_health.review_record(body) is None


MONDAY = date(2026, 11, 2)
STARTS = [MONDAY - timedelta(weeks=1), MONDAY]


@pytest.fixture
def searches(monkeypatch):
    """Each search returns the bodies queued for its week; the calls are recorded."""
    calls, results = [], {}
    monkeypatch.setattr(review_health, "_gh_json", lambda args: {"login": "o"})

    def gh_call(args, timeout):
        week = args[args.index("--merged-at") + 1][:10]
        calls.append(week)
        return results[week]

    monkeypatch.setattr(review_health, "_gh_call", gh_call)
    monkeypatch.setattr(review_health, "_past", {})
    return calls, results


def test_weeks_count_merged_and_reviewed_prs_and_keep_past_weeks(searches):
    calls, results = searches
    results["2026-10-26"] = ([{"body": _body(f"- Codex: FAIL on {A[:7]}, PASS on {B}\n")},
                              {"body": _body(f"- Codex: PASS on {A}\n")},
                              {"body": _body(f"- Independent review: PASS on {A}\n")},
                              {"body": "## Summary\n- docs only\n"},
                              {"body": "## Verification\n- received independent `PASS` on the board\n"},
                              {"body": "## Summary\n- PASS the tests; no reviewer\n"},
                              {"body": "Please review the complete diff in one pass\n"}], GH_OK)
    results["2026-11-02"] = ([], GH_OK)

    first = review_health._fetch(STARTS)
    assert first["weeks"] == [
        {"week_start": "2026-10-26", "merged": 7, "reviewed": 3, "unpinned": 1, "median_rounds": 1,
         "with_fail": 1, "overthinking": 0},
        {"week_start": "2026-11-02", "merged": 0, "reviewed": 0, "unpinned": 0, "median_rounds": None,
         "with_fail": 0, "overthinking": 0},
    ]
    assert review_health._fetch(STARTS) == first
    assert calls == ["2026-10-26", "2026-11-02", "2026-11-02"]  # only the current week is searched again


@pytest.mark.parametrize("result", [(None, GH_ERROR), ([{"body": ""}] * 1000, GH_OK)])
def test_a_failed_or_capped_week_raises_and_the_next_refresh_resumes(searches, result):
    calls, results = searches
    results["2026-10-26"] = ([], GH_OK)
    results["2026-11-02"] = result
    with pytest.raises(ValueError, match="week of 2026-11-02"):
        review_health._fetch(STARTS)
    results["2026-11-02"] = ([], GH_OK)
    assert review_health._fetch(STARTS)["weeks"][1]["merged"] == 0
    assert calls == ["2026-10-26", "2026-11-02", "2026-11-02"]


def test_a_new_week_replaces_the_cached_collection(monkeypatch):
    made = []

    class FakeCache:
        def __init__(self, **_):
            made.append(self)

        def read(self, fetch):
            return {"cache": made.index(self)}

    monkeypatch.setattr(review_health, "GitHubCache", FakeCache)
    monkeypatch.setattr(review_health, "_cache", {"key": None, "cache": None})
    assert review_health.review_health() == {"cache": 0}
    assert review_health.review_health() == {"cache": 0}
    monkeypatch.setitem(review_health._cache, "key", ["an older week"])
    assert review_health.review_health() == {"cache": 1}
