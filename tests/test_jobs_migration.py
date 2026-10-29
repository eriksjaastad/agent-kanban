"""The jobs migration preserves existing data and creates local-only tables."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.migration_runner import discover_migrations  # noqa: E402
from db.schema import schema_statements  # noqa: E402


MIGRATIONS_DIR = Path(__file__).parent.parent / "scripts" / "db" / "migrations"


def _migration_up(conn: sqlite3.Connection, version: int = 13) -> None:
    migration = next(
        m for m in discover_migrations(MIGRATIONS_DIR) if m.version == version
    )
    migration.up(conn)


def test_jobs_migration_is_additive_idempotent_and_preserves_existing_data(
    tmp_path: Path,
) -> None:
    original = sqlite3.connect(tmp_path / "original.db")
    original.execute("CREATE TABLE existing_record (value TEXT NOT NULL)")
    original.execute("INSERT INTO existing_record VALUES ('retained')")
    original.commit()
    copy = sqlite3.connect(tmp_path / "copy.db")
    original.backup(copy)
    original.close()

    _migration_up(copy)
    _migration_up(copy)
    assert copy.execute("SELECT value FROM existing_record").fetchall() == [
        ("retained",)
    ]
    assert {
        row[0]
        for row in copy.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } >= {"jobs", "job_submissions", "existing_record"}
    copy.close()


def test_jobs_constraints_and_repeat_submissions(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("PRAGMA foreign_keys = ON")
    _migration_up(conn)
    conn.execute(
        "INSERT INTO jobs (company, title, url) VALUES (?, ?, ?)",
        ("Example", "Engineer", "https://example.test/jobs/1"),
    )
    job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
    conn.executemany(
        "INSERT INTO job_submissions (job_id, submitted_at) VALUES (?, ?)",
        [(job_id, "2026-09-22T12:00:00Z"), (job_id, "2026-09-23T12:00:00Z")],
    )
    _migration_up(conn)
    assert conn.execute(
        "SELECT submitted_at FROM job_submissions ORDER BY submitted_at"
    ).fetchall() == [
        ("2026-09-22T12:00:00Z",),
        ("2026-09-23T12:00:00Z",),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (company, title, url) VALUES (?, ?, ?)",
            ("Other", "Engineer", "https://example.test/jobs/1"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (company, title, url, category) VALUES (?, ?, ?, ?)",
            ("Other", "Engineer", "https://example.test/jobs/2", "Unbounded"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_submissions (job_id, submitted_at) VALUES (?, ?)",
            (999, "2026-09-23T12:00:00Z"),
        )
    assert conn.execute("PRAGMA foreign_key_list(job_submissions)").fetchall() == [
        (0, 0, "jobs", "job_id", "id", "NO ACTION", "NO ACTION", "NONE")
    ]
    assert conn.execute("SELECT COUNT(*) FROM job_submissions").fetchone()[0] == 2
    conn.close()


def test_fresh_schema_matches_migration() -> None:
    conn = sqlite3.connect(":memory:")
    for statement in schema_statements():
        conn.execute(statement)
    _migration_up(conn)
    _migration_up(conn, 16)  # a fresh schema already has kind: ALTERs skip
    migrated = sqlite3.connect(":memory:")
    _migration_up(migrated)
    _migration_up(migrated, 16)
    for table in ("jobs", "job_submissions"):
        assert conn.execute(f"PRAGMA table_info({table})").fetchall() == (
            migrated.execute(f"PRAGMA table_info({table})").fetchall()
        )
        # index_list follows creation order, which differs between the paths.
        assert sorted(r[1:] for r in conn.execute(f"PRAGMA index_list({table})")) == (
            sorted(r[1:] for r in migrated.execute(f"PRAGMA index_list({table})"))
        )
    conn.close()
    migrated.close()


def _legacy_jobs_db(path: Path) -> sqlite3.Connection:
    """A jobs table as migration 013 left it, with hand-entered volunteer rows."""
    conn = sqlite3.connect(path)
    _migration_up(conn)
    conn.executemany(
        "INSERT INTO jobs (company, title, url, source) VALUES (?, ?, ?, ?)",
        [
            ("Husky Crew", "Volunteer: Rowing coach", "https://example.test/v1", "manual"),
            ("Food Bank", "volunteer: Website help", "https://example.test/v2", "manual"),
            ("Acme", "Volunteer: Imported", "https://example.test/v3", "ats_sweep"),
            ("HN", "VOLUNTEER: From HN", "https://example.test/v5", "hn"),
            ("Acme", "Frontend Engineer", "https://example.test/j1", "manual"),
            # A paid role whose title merely starts with the word stays a job.
            ("Org", "Volunteer Coordinator", "https://example.test/j2", "ats_sweep"),
            ("Bare", "Volunteer: ", "https://example.test/v4", "manual"),
        ],
    )
    conn.commit()
    return conn


def test_kind_migration_backfills_volunteer_rows_and_is_idempotent(tmp_path: Path) -> None:
    conn = _legacy_jobs_db(tmp_path / "legacy.db")
    _migration_up(conn, 16)
    first = conn.execute("SELECT url, title, kind, volunteering_since FROM jobs ORDER BY url").fetchall()
    assert first == [
        ("https://example.test/j1", "Frontend Engineer", "job", None),
        ("https://example.test/j2", "Volunteer Coordinator", "job", None),
        ("https://example.test/v1", "Rowing coach", "volunteer", None),
        ("https://example.test/v2", "Website help", "volunteer", None),
        # The prefix marks a volunteer role whatever the source.
        ("https://example.test/v3", "Imported", "volunteer", None),
        # Nothing after the prefix: keep the title rather than blank it.
        ("https://example.test/v4", "Volunteer: ", "volunteer", None),
        ("https://example.test/v5", "From HN", "volunteer", None),
    ]
    _migration_up(conn, 16)
    assert conn.execute(
        "SELECT url, title, kind, volunteering_since FROM jobs ORDER BY url"
    ).fetchall() == first
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO jobs (company, title, url, kind) VALUES ('X', 'Y', 'https://example.test/x', 'gig')"
        )
    conn.close()
