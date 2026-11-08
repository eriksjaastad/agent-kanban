"""The jobs tables: constraints and repeat submissions."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.schema import schema_statements  # noqa: E402


def _schema(conn: sqlite3.Connection) -> None:
    """Build the current schema (schema.sql) on ``conn``."""
    for statement in schema_statements():
        conn.execute(statement)


def test_jobs_constraints_and_repeat_submissions(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "jobs.db")
    conn.execute("PRAGMA foreign_keys = ON")
    _schema(conn)
    conn.execute(
        "INSERT INTO jobs (company, title, url) VALUES (?, ?, ?)",
        ("Example", "Engineer", "https://example.test/jobs/1"),
    )
    job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
    conn.executemany(
        "INSERT INTO job_submissions (job_id, submitted_at) VALUES (?, ?)",
        [(job_id, "2026-09-22T12:00:00Z"), (job_id, "2026-09-23T12:00:00Z")],
    )
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


