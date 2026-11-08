"""Jobs API contract: dismissal preserves history and submissions remain repeatable."""

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app


def _job(db, suffix: str, category: str = "Other") -> dict:
    return db.upsert_job(
        company=f"Company {suffix}", title=f"Engineer {suffix}",
        url=f"https://example.test/jobs/{suffix}", source="manual",
        category=category, raw=f"Original posting {suffix}",
    )


def test_open_jobs_excludes_submitted_and_dismissed_without_erasing_rows(db):
    client = TestClient(app)
    open_job = _job(db, "open", "Frontend/React")
    submitted = _job(db, "submitted")
    dismissed = _job(db, "dismissed")

    response = client.post(
        f"/api/jobs/{submitted['id']}/submissions",
        json={"submitted_at": "2026-09-22T15:00:00+00:00"},
    )
    assert response.status_code == 201, response.text
    response = client.delete(f"/api/jobs/{dismissed['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["job"]["deleted_at"]

    response = client.get("/api/jobs")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["jobs"]] == [open_job["id"]]
    assert client.get("/api/jobs/stats").json()["categories"] == [
        {"category": "Frontend/React", "count": 1}
    ]

    # The job that moved out of Listings is the one now on /jobs/submitted,
    # carrying the submission timestamp it was recorded with.
    submitted_groups = client.get("/api/jobs/submissions").json()["jobs"]
    assert [group["job"]["id"] for group in submitted_groups] == [submitted["id"]]
    assert [s["submitted_at"] for s in submitted_groups[0]["submissions"]] == [
        "2026-09-22T15:00:00+00:00"
    ]
    with db._get_conn() as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM jobs")}
    assert ids == {open_job["id"], submitted["id"], dismissed["id"]}

    # A refresh cannot silently resurrect a dismissed listing.
    refreshed = _job(db, "dismissed")
    assert refreshed["id"] == dismissed["id"]
    assert refreshed["deleted_at"] is not None


def test_jobs_list_leaves_out_staff_or_principal_only_titles(db):
    """Owner decision 2026-10-15: Staff/Principal-only roles stay off; other Staff uses and mixed levels show."""
    titles = ["Staff/Principal Software Engineer", "Senior/Staff Engineer", "Staff Accountant"]
    for n, title in enumerate(titles):
        db.upsert_job(company="Co", title=title, url=f"https://example.test/level/{n}", source="hn")
    client = TestClient(app)
    listed = client.get("/api/jobs").json()
    assert {row["title"] for row in listed["jobs"] + listed["stale"]} == set(titles[1:])
    assert client.get("/api/jobs/stats").json()["categories"] == [{"category": "Other", "count": 2}]


def test_repeat_submissions_are_grouped_newest_first_and_counted(db):
    client = TestClient(app)
    job = _job(db, "repeat", "Backend")
    for timestamp in ("2026-09-20T12:00:00+00:00", "2026-09-23T12:00:00+00:00"):
        response = client.post(
            f"/api/jobs/{job['id']}/submissions",
            json={"submitted_at": timestamp, "resume_path": "applications/repeat/resume.md"},
        )
        assert response.status_code == 201, response.text

    groups = client.get("/api/jobs/submissions").json()["jobs"]
    assert len(groups) == 1
    assert groups[0]["job"]["id"] == job["id"]
    assert [s["submitted_at"][:10] for s in groups[0]["submissions"]] == [
        "2026-09-23", "2026-09-20"
    ]
    assert groups[0]["submissions"][0]["id"] != groups[0]["submissions"][1]["id"]
    assert all(s["resume_path"] == "applications/repeat/resume.md" for s in groups[0]["submissions"])

    stats = client.get("/api/jobs/stats").json()
    assert stats["submissions_per_day"] == [
        {"date": "2026-09-20", "count": 1},
        {"date": "2026-09-23", "count": 1},
    ]
    assert stats["categories"] == []  # submitted jobs are no longer open prospects
    assert sum(row["count"] for row in stats["jobs_per_day"]) == 1


def test_unknown_and_invalid_submissions_do_not_create_history(db):
    client = TestClient(app)
    job = _job(db, "validation")
    assert client.delete("/api/jobs/999999").status_code == 404
    assert client.post("/api/jobs/999999/submissions", json={}).status_code == 404
    bad = client.post(
        f"/api/jobs/{job['id']}/submissions", json={"submitted_at": "not-a-date"}
    )
    assert bad.status_code == 400
    assert client.get("/api/jobs/submissions").json()["jobs"] == []


def test_submissions_order_by_instant_across_different_utc_offsets(db):
    client = TestClient(app)
    later = _job(db, "later")
    earlier = _job(db, "earlier")
    # Lexical order disagrees with real time: the -07:00 entry is 06:30Z.
    for job, timestamp in (
        (later, "2026-09-22T23:30:00-07:00"),
        (earlier, "2026-09-23T04:00:00+00:00"),
    ):
        assert client.post(
            f"/api/jobs/{job['id']}/submissions", json={"submitted_at": timestamp}
        ).status_code == 201
    groups = client.get("/api/jobs/submissions").json()["jobs"]
    assert [group["job"]["id"] for group in groups] == [later["id"], earlier["id"]]
    assert groups[0]["submissions"][0]["submitted_at"] == "2026-09-23T06:30:00+00:00"


def test_upsert_preserves_first_seen_and_rejects_unbounded_categories(db):
    first = _job(db, "same", "Other")
    with db._get_conn() as conn:
        conn.execute("UPDATE jobs SET first_seen = ? WHERE id = ?", ("2026-09-01", first["id"]))
        conn.commit()
    revised = db.upsert_job(
        company="Revised Company", title="Revised role",
        url=first["url"], source="manual", category="SEO",
    )
    assert revised["id"] == first["id"]
    assert revised["first_seen"] == "2026-09-01"
    assert revised["company"] == "Revised Company"
    assert revised["category"] == "SEO"
    assert revised["raw"] == first["raw"]

    import pytest
    with pytest.raises(ValueError, match="invalid job category"):
        db.upsert_job(company="A", title="B", url="https://example.test/new", source="manual", category="Novel")


RULES = "House rules:\n- Fork the base resume into applications/{company}/\n- Never invent a fact\n"


def _rules(tmp_path, monkeypatch, text=RULES, section='[jobs]\ntailor_rules = "rules.md"\n'):
    """A config whose [jobs] tailor_rules names rules.md beside it, relative as documented."""
    (tmp_path / "rules.md").write_text(text, encoding="utf-8")
    (tmp_path / "config.toml").write_text(section, encoding="utf-8")
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))


def _queued_prompt(client, job, monkeypatch):
    import shlex
    from discovery.agent_registry import CommandResult

    calls = []
    monkeypatch.setattr("dashboard.routes.jobs.run_agent_command", lambda *call: calls.append(call) or CommandResult(
        success=True, output="", error="", return_code=0, duration_ms=1, command=""))
    response = client.post(f"/api/jobs/{job['id']}/agent-prompt")
    return response, calls and shlex.split(calls[0][2])[1]


def test_agent_prompt_carries_the_configured_rules(db, tmp_path, monkeypatch):
    job = _job(db, "rules")
    _rules(tmp_path, monkeypatch)
    response, prompt = _queued_prompt(TestClient(app), job, monkeypatch)
    assert response.status_code == 200, response.text
    assert prompt == (f"Tailor resume and cover letter for this job posting:\n\nCompany: Company rules\n"
                      f"Title: Engineer rules\nURL: {job['url']}\n\nHouse rules:\n"
                      "- Fork the base resume into applications/Company rules/\n- Never invent a fact\n\n"
                      "Review the posting at the URL above and create tailored materials following these rules.")


@pytest.mark.parametrize("text", ["", "  \n\t\n"], ids=["empty", "whitespace"])
def test_a_configured_but_empty_rules_file_is_still_the_rules(db, tmp_path, monkeypatch, text):
    job = _job(db, "empty")
    _rules(tmp_path, monkeypatch, text=text)
    response, prompt = _queued_prompt(TestClient(app), job, monkeypatch)
    assert response.status_code == 200, response.text
    assert prompt == (f"Tailor resume and cover letter for this job posting:\n\nCompany: Company empty\n"
                      f"Title: Engineer empty\nURL: {job['url']}\n\n\n\n"
                      "Review the posting at the URL above and create tailored materials following these rules.")


def test_agent_prompt_without_rules_names_none(db, monkeypatch):
    job = _job(db, "plain")
    response, prompt = _queued_prompt(TestClient(app), job, monkeypatch)
    assert response.status_code == 200, response.text
    assert prompt.endswith(f"URL: {job['url']}\n\nReview the posting at the URL above and create tailored materials.")


@pytest.mark.parametrize("section, detail", [
    ('[jobs]\ntailor_rules = "missing.md"\n', "missing.md"),
    ('[jobs]\ntailor_rules = "rules.md/"\n', "Not a directory"),
    ('[jobs]\ntailor_rules = " rules.md"\n', "[jobs] tailor_rules must be"),
    ('[jobs]\nrules = "rules.md"\n', "[jobs] may only set"),
])
def test_unloadable_rules_queue_nothing(db, tmp_path, monkeypatch, section, detail):
    job = _job(db, "broken")
    _rules(tmp_path, monkeypatch, section=section)
    response, prompt = _queued_prompt(TestClient(app), job, monkeypatch)
    assert response.status_code == 503 and detail in response.json()["detail"] and not prompt


def test_agent_prompt_endpoint_composes_correct_prompt(db, tmp_path, monkeypatch):
    """Agent prompt endpoint queues message with job details and house rules (card #7400)."""
    import shlex
    from discovery.agent_registry import CommandResult, get_agent_command
    
    client = TestClient(app)
    job = _job(db, "agent-test", "Backend")
    _rules(tmp_path, monkeypatch)
    
    captured_calls = []
    def mock_run_agent(agent_name, command_name, args):
        captured_calls.append((agent_name, command_name, args))
        return CommandResult(
            success=True, output="Message sent", error="", return_code=0, duration_ms=100, command="pt message send"
        )
    
    monkeypatch.setattr("dashboard.routes.jobs.run_agent_command", mock_run_agent)
    
    response = client.post(f"/api/jobs/{job['id']}/agent-prompt")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["success"] is True
    assert data["job_id"] == job["id"]
    
    assert len(captured_calls) == 1
    agent_name, command_name, args = captured_calls[0]
    assert agent_name == "pt"
    assert command_name == "message"
    
    assert get_agent_command("pt", "message") is not None, "pt message command must be registered"
    
    argv_parts = shlex.split(args)
    assert len(argv_parts) == 2, f"Expected ['send', <prompt>], got {argv_parts}"
    assert argv_parts[0] == "send"
    prompt_body = argv_parts[1]
    
    assert f"Company: Company agent-test" in prompt_body
    assert f"Title: Engineer agent-test" in prompt_body
    assert job["url"] in prompt_body
    assert "Never invent a fact" in prompt_body


def test_agent_prompt_handles_special_chars_in_company_title(db, monkeypatch):
    """Prompt quoting must handle embedded quotes and special characters safely."""
    import shlex
    from discovery.agent_registry import CommandResult
    
    client = TestClient(app)
    job = db.upsert_job(
        company='Tech "Innovators" Inc.',
        title='Senior $Engineer (Full-Stack)',
        url="https://example.test/special-chars",
        source="manual",
        category="Other",
    )
    
    captured_calls = []
    def mock_run_agent(agent_name, command_name, args):
        captured_calls.append((agent_name, command_name, args))
        return CommandResult(
            success=True, output="", error="", return_code=0, duration_ms=10, command=""
        )
    
    monkeypatch.setattr("dashboard.routes.jobs.run_agent_command", mock_run_agent)
    
    response = client.post(f"/api/jobs/{job['id']}/agent-prompt")
    assert response.status_code == 200, response.text
    
    agent_name, command_name, args = captured_calls[0]
    argv_parts = shlex.split(args)
    assert len(argv_parts) == 2
    assert argv_parts[0] == "send"
    prompt = argv_parts[1]
    
    assert 'Tech "Innovators" Inc.' in prompt
    assert 'Senior $Engineer (Full-Stack)' in prompt


def test_agent_prompt_rejects_nonexistent_job(db):
    client = TestClient(app)
    response = client.post("/api/jobs/999999/agent-prompt")
    assert response.status_code == 404


def test_agent_prompt_rejects_submitted_job(db):
    client = TestClient(app)
    job = _job(db, "submitted-check")
    client.post(
        f"/api/jobs/{job['id']}/submissions",
        json={"submitted_at": "2026-09-25T10:00:00+00:00"},
    )
    response = client.post(f"/api/jobs/{job['id']}/agent-prompt")
    assert response.status_code == 404


def test_agent_prompt_rejects_dismissed_job(db):
    client = TestClient(app)
    job = _job(db, "dismissed-check")
    client.delete(f"/api/jobs/{job['id']}")
    response = client.post(f"/api/jobs/{job['id']}/agent-prompt")
    assert response.status_code == 404


def test_agent_prompt_handles_command_failure(db, monkeypatch):
    from discovery.agent_registry import CommandResult
    
    client = TestClient(app)
    job = _job(db, "cmd-failure")
    
    def mock_run_agent(agent_name, command_name, args):
        return CommandResult(
            success=False, output="", error="Agent not available", return_code=1, duration_ms=10, command=""
        )
    
    monkeypatch.setattr("dashboard.routes.jobs.run_agent_command", mock_run_agent)
    
    response = client.post(f"/api/jobs/{job['id']}/agent-prompt")
    assert response.status_code == 500
    assert "Failed to queue agent prompt" in response.json()["detail"]
