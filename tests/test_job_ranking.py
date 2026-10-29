"""Fit score, ranking and the 30-day stale split (#101679546294759424)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from hypothesis import example, given, settings
from hypothesis import strategies as st

from dashboard.app import app
from scripts.db.job_ranking import (
    rank_listings,
    score_listing,
    split_stale,
)

TODAY = date(2026, 10, 8)
# The fit keywords come from the local config's [jobs] fit_keywords; these stand in.
KEYWORDS = ("react", "frontend", "wordpress", "typescript", "python", "ai")


def _row(job_id: int, title: str, *, raw: str = "", url: str | None = None,
         posted: date | None = TODAY, first_seen: date = TODAY) -> dict:
    return {
        "id": job_id, "title": title, "raw": raw, "location": None,
        "url": url or f"https://example.test/{job_id}",
        "posted_date": posted.isoformat() if posted else None,
        "first_seen": first_seen.isoformat(),
    }


def test_relevant_listing_outranks_unrelated_one() -> None:
    react = _row(1, "React Developer", raw="WordPress and TypeScript")
    wordpress = _row(2, "WordPress site maintainer")
    unrelated = _row(3, "Warehouse Associate", raw="Forklift certification")
    ranked = rank_listings([unrelated, wordpress, react], TODAY, KEYWORDS)
    assert [job["id"] for job in ranked] == [1, 2, 3]
    assert ranked[0]["fit_score"] > ranked[2]["fit_score"]


def test_title_matches_weigh_more_than_raw_matches() -> None:
    in_title = _row(1, "Python engineer")
    in_raw = _row(2, "Engineer", raw="We use Python")
    assert score_listing(in_title, TODAY, KEYWORDS) > score_listing(in_raw, TODAY, KEYWORDS)


def test_keywords_match_whole_words_case_insensitively() -> None:
    assert score_listing(_row(1, "Senior AI Engineer"), TODAY, KEYWORDS) > score_listing(
        _row(2, "Senior Engineer"), TODAY, KEYWORDS
    )
    # "ai" must not fire inside "said" or "maintain".
    assert score_listing(_row(3, "Maintainer", raw="he said"), TODAY, KEYWORDS) == score_listing(
        _row(4, "Plumber", raw="he spoke"), TODAY, KEYWORDS
    )


_WORD = st.text(st.sampled_from("abcAB -"), min_size=1, max_size=4).filter(lambda w: w == w.strip())


@settings(max_examples=200, deadline=None)
@given(keywords=st.lists(_WORD, max_size=4), title=st.text(st.sampled_from("abcAB -"), max_size=12),
       raw=st.text(st.sampled_from("abcAB -"), max_size=12))
@example(keywords=["react"], title="React dev", raw="react")
def test_case_variants_and_iterators_never_change_a_score(keywords, title, raw) -> None:
    job = {**_row(1, title), "raw": raw}
    score = score_listing(job, TODAY, tuple(keywords))
    variants = keywords + [k.upper() for k in keywords] + [k.lower() for k in keywords] + keywords
    assert score_listing(job, TODAY, variants) == score
    assert score_listing(job, TODAY, iter(keywords)) == score
    assert score_listing(job, TODAY, (k for k in keywords)) == score
    assert rank_listings([job], TODAY, iter(keywords))[0]["fit_score"] == score


def test_no_keywords_means_no_fit_boost() -> None:
    react = _row(1, "React Developer", raw="WordPress and TypeScript")
    assert score_listing(react, TODAY) == score_listing(_row(2, "Warehouse Associate"), TODAY)


def test_pay_and_remote_us_signals_raise_score() -> None:
    plain = _row(1, "Support specialist")
    paid = _row(2, "Support specialist", raw="$45/hr, remote (US)")
    assert score_listing(paid, TODAY) - score_listing(plain, TODAY) == 4


def test_hn_full_post_and_review_rows_rank_below_role_rows() -> None:
    raw = "Acme | React engineer | Remote US | $150k"
    post = _row(1, "Full HN post (read for every role)", raw=raw,
                url="https://news.ycombinator.com/item?id=1#post")
    role = _row(2, "React engineer", raw=raw,
                url="https://news.ycombinator.com/item?id=1#role-1")
    review = _row(3, "[REVIEW] React engineer", raw=raw)
    ranked = rank_listings([post, review, role], TODAY, KEYWORDS)
    assert [job["id"] for job in ranked] == [2, 3, 1]


def test_seniority_words_do_not_demote() -> None:
    assert score_listing(_row(1, "Staff React Engineer"), TODAY, KEYWORDS) == score_listing(
        _row(2, "React Engineer"), TODAY, KEYWORDS
    )


def test_ties_break_newest_first() -> None:
    older = _row(1, "Sales", posted=TODAY - timedelta(days=20))
    newer = _row(2, "Sales", posted=TODAY - timedelta(days=16))
    assert score_listing(older, TODAY) == score_listing(newer, TODAY)
    assert [job["id"] for job in rank_listings([older, newer], TODAY)] == [2, 1]


def test_stale_split_at_thirty_day_boundary() -> None:
    edge = _row(1, "Edge", posted=TODAY - timedelta(days=30))
    stale = _row(2, "Stale", posted=TODAY - timedelta(days=31))
    # posted_date absent: first_seen decides.
    seen_old = _row(3, "Seen old", posted=None, first_seen=TODAY - timedelta(days=31))
    # posted_date wins over a recent first_seen.
    posted_old = _row(4, "Posted old", posted=TODAY - timedelta(days=45))
    undated = {**_row(5, "Undated", posted=None), "first_seen": "not-a-date"}
    fresh, old = split_stale([edge, stale, seen_old, posted_old, undated], TODAY)
    assert [job["id"] for job in fresh] == [1, 5]
    assert [job["id"] for job in old] == [2, 3, 4]


def test_jobs_api_returns_ranked_fresh_and_stale_separately(db, tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[jobs]\nfit_keywords = ["react", "frontend"]\n', encoding="utf-8")
    monkeypatch.setenv("PT_CONFIG", str(config))
    today = date.today()
    old = db.upsert_job(company="Old", title="React engineer", source="manual",
                        url="https://example.test/old",
                        posted_date=(today - timedelta(days=60)).isoformat())
    plain = db.upsert_job(company="Plain", title="Account executive", source="manual",
                          url="https://example.test/plain")
    fit = db.upsert_job(company="Fit", title="Frontend developer (React)", source="manual",
                        url="https://example.test/fit")
    body = TestClient(app).get("/api/jobs").json()
    assert [job["id"] for job in body["jobs"]] == [fit["id"], plain["id"]]
    assert [job["id"] for job in body["stale"]] == [old["id"]]
    assert body["jobs"][0]["fit_score"] > body["jobs"][1]["fit_score"]


def _remote_points(location: str) -> int:
    plain = score_listing(_row(1, "Support specialist"), TODAY)
    return score_listing({**_row(2, "Support specialist"), "location": location}, TODAY) - plain


def test_remote_us_points_need_remote_wording_next_to_the_us_phrase() -> None:
    for remote_us in ("Remote (US)", "Remote - United States", "US remote",
                      "Fully remote; candidates must be US-based", "Remote (U.S.)"):
        assert _remote_points(remote_us) == 2, remote_us
    for not_remote_us in ("Seattle, United States", "Hybrid - United States",
                          "On-site in Austin. US-based company", "Remote (Canada)",
                          "work with us remotely"):
        assert _remote_points(not_remote_us) == 0, not_remote_us
