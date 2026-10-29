"""Add local job listings and repeatable submission history for #7396.

These tables describe the laptop's job-listing ingestion and dashboard state.
They are LOCAL_ONLY: no cross-machine sync contract exists for their source
feed or attached resume/letter paths. The same DDL lives in schema.py for a
fresh database; CREATE IF NOT EXISTS makes this migration safe after either
creation path. The foreign key has no cascade so a submission is never removed
as a side effect of deleting a listing. Application deletion is soft only.
"""

from __future__ import annotations

import sqlite3

CRR_TABLES: frozenset[str] = frozenset()


def up(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company     TEXT NOT NULL,
            title       TEXT NOT NULL,
            location    TEXT,
            url         TEXT NOT NULL UNIQUE,
            source      TEXT NOT NULL DEFAULT 'manual'
                        CHECK (source IN ('ats_sweep', 'hn', 'manual')),
            posted_date TEXT,
            first_seen  TEXT NOT NULL DEFAULT (date('now')),
            category    TEXT NOT NULL DEFAULT 'Other'
                        CHECK (category IN (
                            'Frontend/React', 'Full Stack',
                            'Forward Deployed / Solutions', 'SEO',
                            'Backend', 'Other'
                        )),
            raw         TEXT,
            deleted_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_submissions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id            INTEGER NOT NULL REFERENCES jobs(id),
            submitted_at      TEXT NOT NULL,
            resume_path       TEXT,
            cover_letter_path TEXT,
            notes             TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_category ON jobs(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_deleted_at ON jobs(deleted_at)")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_job_submissions_job_submitted
        ON job_submissions(job_id, submitted_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_job_submissions_submitted_at
        ON job_submissions(submitted_at)
    """)
