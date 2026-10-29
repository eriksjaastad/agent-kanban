"""Jobs and Volunteer keep serving before `pt db migrate` applies 016 (#101668605654102016).

Numbered migrations reach an existing database only through `pt db migrate`
(a verified-backup destructive operation); the dashboard never applies them.
A database still in the 013 shape must not turn the Jobs page into a 500.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from db.jobs import JobsMigrationPendingError  # same module object the app raises


@pytest.fixture
def pre_016_db(isolated_database, db):
    """The per-test database rewound to the jobs shape migration 013 left."""
    conn = sqlite3.connect(isolated_database)
    conn.execute("DROP INDEX IF EXISTS idx_jobs_kind")
    conn.execute("ALTER TABLE jobs DROP COLUMN volunteering_since")
    conn.execute("ALTER TABLE jobs DROP COLUMN kind")
    conn.execute("DELETE FROM schema_migrations WHERE version = 16")
    conn.executemany(
        "INSERT INTO jobs (company, title, url, source, category) VALUES (?, ?, ?, ?, ?)",
        [
            ("Acme", "React engineer", "https://example.test/pre/1", "ats_sweep", "Frontend/React"),
            ("Food Bank", "Volunteer: Website help", "https://example.test/pre/2", "manual", "Other"),
        ],
    )
    conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()
    assert "kind" not in columns
    assert [m["version"] for m in db.migrations_pending()] == [16]
    return db


def test_jobs_and_volunteer_endpoints_serve_before_migration_016(pre_016_db) -> None:
    client = TestClient(app)

    jobs = client.get("/api/jobs")
    assert jobs.status_code == 200, jobs.text
    assert {row["url"] for row in jobs.json()["jobs"]} == {
        "https://example.test/pre/1", "https://example.test/pre/2",
    }
    assert client.get("/api/jobs/stats").status_code == 200
    assert client.get("/api/jobs/submissions").status_code == 200

    volunteer = client.get("/api/volunteer")
    assert volunteer.status_code == 200, volunteer.text
    assert volunteer.json() == {
        "migration_pending": True, "current": [], "applied": [],
        "possible": [], "possible_stale": [],
    }
    started = client.post("/api/jobs/1/volunteering")
    assert started.status_code == 409
    assert "pt db migrate" in started.json()["detail"]


def test_upsert_before_migration_016_keeps_jobs_and_refuses_volunteer(pre_016_db) -> None:
    job = pre_016_db.upsert_job(company="New", title="Engineer", source="manual",
                                url="https://example.test/pre/3")
    assert "kind" not in job
    with pytest.raises(JobsMigrationPendingError, match="pt db migrate"):
        pre_016_db.upsert_job(company="Org", title="Helper", source="manual",
                              url="https://example.test/pre/4", kind="volunteer")


def test_dry_run_flags_volunteer_lines_before_migration_016(pre_016_db, tmp_path) -> None:
    """A dry run reports what a real run would reject (rule E1)."""
    jsonl = tmp_path / "dry.jsonl"
    jsonl.write_text(
        json.dumps({"company": "Acme", "title": "Engineer", "url": "https://example.test/dry/1",
                    "source": "manual"}) + "\n"
        + json.dumps({"company": "Food Bank", "title": "Helper", "url": "https://example.test/dry/2",
                      "source": "manual", "kind": "volunteer"}) + "\n"
    )
    reasons = {}
    for mode in (["--dry-run"], []):
        result = subprocess.run(
            [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl), "--json", *mode],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert response["result"]["imported"] == 1
        [error] = response["result"]["details"]["errors"]
        assert error["line"] == 2
        reasons[bool(mode)] = error["reason"]
    assert "pt db migrate" in reasons[True]
    assert reasons[True] == reasons[False]
