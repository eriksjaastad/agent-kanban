"""Add site_stats for #8868 (central website stats).

One row per site per `pt site-stats run`: the collector's exit code and, when
that run wrote a fresh status, each source's status, flags, period and metrics
as JSON. sources is NULL when the run left no new status (a failed run).

Additive and idempotent.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS site_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site TEXT NOT NULL,
            ran_at TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            sources TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_site_stats_site_time ON site_stats(site, ran_at DESC)")
