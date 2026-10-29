"""Volunteer tab (#101668605654102016): sections, transitions, and isolation from Jobs."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app


def _role(db, suffix: str, kind: str = "volunteer", title: str = "", **extra) -> dict:
    return db.upsert_job(
        company=f"Org {suffix}", title=title or f"Role {suffix}",
        url=f"https://example.test/volunteer/{suffix}", source="manual",
        kind=kind, **extra,
    )


def test_volunteer_rows_never_reach_jobs_listings_stats_or_submissions(db) -> None:
    client = TestClient(app)
    job = _role(db, "job", kind="job", category="Frontend/React")
    possible = _role(db, "possible", category="Frontend/React")
    applied = _role(db, "applied")
    assert client.post(f"/api/jobs/{applied['id']}/submissions", json={
        "submitted_at": "2026-10-01T12:00:00+00:00"}).status_code == 201

    body = client.get("/api/jobs").json()
    assert [row["id"] for row in body["jobs"] + body["stale"]] == [job["id"]]
    assert client.get("/api/jobs/submissions").json()["jobs"] == []
    stats = client.get("/api/jobs/stats").json()
    assert stats["categories"] == [{"category": "Frontend/React", "count": 1}]
    assert stats["submissions_per_day"] == []
    assert sum(row["count"] for row in stats["jobs_per_day"]) == 1
    # Volunteer roles cannot be sent to the resume-tailoring agent either.
    assert client.post(f"/api/jobs/{possible['id']}/agent-prompt").status_code == 404


def test_volunteer_sections_and_started_volunteering_transition(db, tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[jobs]\nfit_keywords = ["wordpress"]\n', encoding="utf-8")
    monkeypatch.setenv("PT_CONFIG", str(config))
    client = TestClient(app)
    _role(db, "job", kind="job")
    possible = _role(db, "possible", title="Website volunteer (WordPress)")
    other = _role(db, "other")
    dismissed = _role(db, "dismissed")
    old = _role(db, "old", posted_date=(date.today() - timedelta(days=45)).isoformat())
    applied = _role(db, "applied")
    client.delete(f"/api/jobs/{dismissed['id']}")
    client.post(f"/api/jobs/{applied['id']}/submissions", json={})

    body = client.get("/api/volunteer").json()
    assert body["current"] == []
    assert [row["id"] for row in body["applied"]] == [applied["id"]]
    assert body["applied"][0]["last_submitted_at"]
    # Ranked: the WordPress role outranks the plain one.
    assert [row["id"] for row in body["possible"]] == [possible["id"], other["id"]]
    assert [row["id"] for row in body["possible_stale"]] == [old["id"]]

    response = client.post(f"/api/jobs/{applied['id']}/volunteering")
    assert response.status_code == 200, response.text
    since = response.json()["job"]["volunteering_since"]
    assert since

    body = client.get("/api/volunteer").json()
    assert [row["id"] for row in body["current"]] == [applied["id"]]
    assert body["applied"] == []

    # Repeating the click keeps the original start date.
    again = client.post(f"/api/jobs/{applied['id']}/volunteering").json()["job"]
    assert again["volunteering_since"] == since


@pytest.mark.parametrize("target", ["job", "missing", "dismissed"])
def test_started_volunteering_rejects_non_volunteer_rows(db, target: str) -> None:
    client = TestClient(app)
    if target == "job":
        job_id = _role(db, "job", kind="job")["id"]
    elif target == "dismissed":
        job_id = _role(db, "dismissed")["id"]
        assert client.delete(f"/api/jobs/{job_id}").status_code == 200
    else:
        job_id = 999_999
    assert client.post(f"/api/jobs/{job_id}/volunteering").status_code == 404
    if target == "job":
        assert db.get_open_jobs()[0]["volunteering_since"] is None
    if target == "dismissed":
        with db._get_conn() as conn:
            row = conn.execute(
                "SELECT volunteering_since FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row[0] is None


def test_upsert_rejects_unknown_kind_and_keeps_kind_when_omitted(db) -> None:
    with pytest.raises(ValueError, match="kind"):
        _role(db, "bad", kind="gig")
    role = _role(db, "keep")
    refreshed = db.upsert_job(company="Org keep", title="Role keep", source="manual",
                              url=role["url"])
    assert refreshed["kind"] == "volunteer"


def test_dismissed_volunteer_roles_leave_every_section(db) -> None:
    client = TestClient(app)
    applied = _role(db, "applied")
    current = _role(db, "current")
    for role in (applied, current):
        assert client.post(f"/api/jobs/{role['id']}/submissions", json={}).status_code == 201
    assert client.post(f"/api/jobs/{current['id']}/volunteering").status_code == 200
    for role in (applied, current):
        assert client.delete(f"/api/jobs/{role['id']}").status_code == 200

    body = client.get("/api/volunteer").json()
    assert body == {"migration_pending": False, "current": [], "applied": [], "possible": [], "possible_stale": []}


def test_possible_role_goes_straight_to_current_without_an_application(db) -> None:
    client = TestClient(app)
    role = _role(db, "already")
    assert client.post(f"/api/jobs/{role['id']}/volunteering").status_code == 200
    body = client.get("/api/volunteer").json()
    assert [row["id"] for row in body["current"]] == [role["id"]]
    assert body["applied"] == [] and body["possible"] == [] and body["possible_stale"] == []


# Every undismissed row lands in exactly one place across both pages.
# (kind, volunteering_since set, has submission) -> expected place.
_STATE_MATRIX = [
    ("job", False, False, "jobs"),
    ("job", False, True, "submissions"),
    # Not reachable through the API any more, but a job must never vanish.
    ("job", True, False, "jobs"),
    ("job", True, True, "submissions"),
    ("volunteer", False, False, "possible"),
    ("volunteer", False, True, "applied"),
    ("volunteer", True, False, "current"),
    ("volunteer", True, True, "current"),
]


def _places(client: TestClient) -> dict[str, list[int]]:
    jobs = client.get("/api/jobs").json()
    volunteer = client.get("/api/volunteer").json()
    return {
        "jobs": [row["id"] for row in jobs["jobs"] + jobs["stale"]],
        "submissions": [g["job"]["id"] for g in client.get("/api/jobs/submissions").json()["jobs"]],
        "current": [row["id"] for row in volunteer["current"]],
        "applied": [row["id"] for row in volunteer["applied"]],
        "possible": [row["id"] for row in volunteer["possible"] + volunteer["possible_stale"]],
    }


@pytest.mark.parametrize("kind,since,submitted,expected", _STATE_MATRIX)
def test_every_undismissed_state_appears_in_exactly_one_place(
    db, kind: str, since: bool, submitted: bool, expected: str,
) -> None:
    client = TestClient(app)
    row = _role(db, "state", kind=kind)
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET volunteering_since = ? WHERE id = ?",
            ("2026-09-01T00:00:00+00:00" if since else None, row["id"]),
        )
        conn.commit()
    if submitted:
        assert client.post(f"/api/jobs/{row['id']}/submissions", json={}).status_code == 201
    places = _places(client)
    assert [place for place, ids in places.items() if row["id"] in ids] == [expected]


def test_reimport_as_job_moves_a_current_volunteer_role_to_jobs(db) -> None:
    client = TestClient(app)
    role = _role(db, "switch")
    client.post(f"/api/jobs/{role['id']}/volunteering")

    # Neighbours: re-importing without kind, or as volunteer, keeps the date.
    for kind in (None, "volunteer"):
        kept = db.upsert_job(company="Org switch", title="Role switch", source="manual",
                             url=role["url"], kind=kind)
        assert kept["volunteering_since"] and kept["kind"] == "volunteer"

    switched = db.upsert_job(company="Org switch", title="Role switch", source="manual",
                             url=role["url"], kind="job")
    assert switched["kind"] == "job" and switched["volunteering_since"] is None
    places = _places(client)
    assert places["jobs"] == [role["id"]]
    assert places["current"] == [] and places["applied"] == [] and places["possible"] == []


@pytest.mark.parametrize("field,value", [
    ("kind", []), ("kind", {"a": 1}), ("source", ["hn"]), ("category", {"SEO": 1}),
    ("company", 5), ("title", None), ("url", ["https://example.test/x"]),
])
def test_upsert_rejects_wrong_types_with_value_error(db, field: str, value) -> None:
    fields = {"company": "Org", "title": "Role", "url": "https://example.test/types",
              "source": "manual", field: value}
    with pytest.raises(ValueError):
        db.upsert_job(**fields)


def test_volunteer_sections_come_from_one_snapshot(db, monkeypatch) -> None:
    """Another tab's write landing between the section queries cannot drop a role."""
    from contextlib import contextmanager

    role = _role(db, "snap")
    real_get_conn = db._get_conn

    def other_tab_starts_volunteering() -> None:
        with real_get_conn() as other:
            other.execute(
                "UPDATE jobs SET volunteering_since = '2026-10-12T00:00:00+00:00' WHERE id = ?",
                (role["id"],),
            )
            other.commit()

    class WriteAfterCurrentQuery:
        """Delegates to the real connection; fires the other tab's write once."""

        def __init__(self, conn) -> None:
            self._conn = conn
            self.fired = False

        def execute(self, sql, *args):
            cursor = self._conn.execute(sql, *args)
            if not self.fired and "IS NOT NULL" in sql and "volunteering_since" in sql:
                cursor = cursor.fetchall()  # the Current query has run
                self.fired = True
                other_tab_starts_volunteering()
                return _Rows(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self._conn, name)

    class _Rows:
        def __init__(self, rows) -> None:
            self._rows = rows

        def fetchall(self):
            return self._rows

    proxies = []

    @contextmanager
    def proxied_conn():
        with real_get_conn() as conn:
            proxy = WriteAfterCurrentQuery(conn)
            proxies.append(proxy)
            yield proxy

    monkeypatch.setattr(db, "_get_conn", proxied_conn)
    sections = db.get_volunteer_roles()
    monkeypatch.undo()

    assert proxies and proxies[0].fired
    placements = [name for name in ("current", "applied", "possible", "possible_stale")
                  if role["id"] in {row["id"] for row in sections[name]}]
    # The snapshot predates the other tab's write: the role is still Possible.
    assert placements == ["possible"]
    # And the write did land: the next load shows it as Current.
    assert role["id"] in {row["id"] for row in db.get_volunteer_roles()["current"]}
