"""Migration 012 — tasks.archived_at (#6870)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.migration_runner import (  # noqa: E402
    apply_migration,
    discover_migrations,
)
from db.schema import create_database  # noqa: E402


MIGRATIONS_DIR = Path(__file__).parent.parent / "scripts" / "db" / "migrations"


def _migration_012():
    [migration] = [
        m for m in discover_migrations(MIGRATIONS_DIR, verbose=False) if m.version == 12
    ]
    return migration


def test_012_adds_archived_at() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT)")

    _migration_012().up(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "archived_at" in cols


def test_012_is_a_noop_when_column_already_exists(tmp_path: Path) -> None:
    """Fresh databases get archived_at from the CREATE TABLE in schema.py,
    so the migration has to tolerate the column already being there."""
    db_path = tmp_path / "tracker.db"
    create_database(db_path)

    conn = sqlite3.connect(db_path)
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "archived_at" in cols_before, "schema.py should create the column"

    _migration_012().up(conn)  # must not raise "duplicate column name"

    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert cols_after == cols_before


def test_012_skips_when_tasks_table_does_not_exist() -> None:
    """`pt db migrate` can run against a bare database file, before
    schema.py has created any tables."""
    conn = sqlite3.connect(":memory:")

    _migration_012().up(conn)  # must not raise "no such table: tasks"

    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        is None
    )


def test_runner_applies_012_and_records_it(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "plain.db", isolation_level=None)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )

    apply_migration(conn, _migration_012())

    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "archived_at" in cols
