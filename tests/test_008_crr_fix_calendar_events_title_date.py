"""Tests for migration 008 — add DEFAULT '' to calendar_events.title and event_date."""

from __future__ import annotations

import importlib.util
import io
import contextlib
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_MIGRATION_PATH = REPO / "scripts" / "db" / "migrations" / "008_crr_fix_calendar_events_title_date.py"
_spec = importlib.util.spec_from_file_location("_m008", _MIGRATION_PATH)
assert _spec and _spec.loader
_m008 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m008)  # type: ignore[union-attr]
up = _m008.up


def _db_with_broken_schema(tmp_path: Path) -> sqlite3.Connection:
    """calendar_events as on the Mini — title and event_date NOT NULL, no DEFAULT."""
    conn = sqlite3.connect(str(tmp_path / "tracker.db"))
    conn.executescript("""
        CREATE TABLE "calendar_events" (
            id                    INTEGER PRIMARY KEY NOT NULL,
            title                 TEXT    NOT NULL,
            description           TEXT,
            event_date            TEXT    NOT NULL,
            event_time            TEXT,
            event_type            TEXT    NOT NULL DEFAULT 'reminder',
            recurrence            TEXT,
            project_id            TEXT,
            machine               TEXT,
            prompt                TEXT,
            notify_before_minutes INTEGER NOT NULL DEFAULT 60,
            notified_at           TEXT,
            status                TEXT    NOT NULL DEFAULT 'active',
            created_by            TEXT,
            metadata              TEXT    DEFAULT '{}',
            created_at            TEXT    NOT NULL DEFAULT '',
            updated_at            TEXT    NOT NULL DEFAULT ''
        );
        INSERT INTO calendar_events
            (id, title, event_date, event_time, event_type, status, created_at, updated_at)
            VALUES (1, 'stand-up', '2026-04-21', '09:00', 'reminder', 'active', '2026-01-01', '2026-01-01');
    """)
    conn.commit()
    return conn


def _db_already_compliant(tmp_path: Path) -> sqlite3.Connection:
    """calendar_events already has DEFAULT '' on title and event_date — no-op case."""
    conn = sqlite3.connect(str(tmp_path / "tracker.db"))
    conn.executescript("""
        CREATE TABLE calendar_events (
            id         INTEGER PRIMARY KEY NOT NULL,
            title      TEXT NOT NULL DEFAULT '',
            event_date TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO calendar_events (id, title, event_date, created_at, updated_at)
            VALUES (1, 'event', '2026-04-21', '2026-01-01', '2026-01-01');
    """)
    conn.commit()
    return conn


def _get_col_defaults(conn: sqlite3.Connection, tbl: str) -> dict[str, str | None]:
    return {r[1]: r[4] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}


def test_defaults_added_to_broken_schema(tmp_path: Path) -> None:
    conn = _db_with_broken_schema(tmp_path)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    defaults = _get_col_defaults(conn, "calendar_events")
    assert defaults["title"] == "''", f"title DEFAULT: {defaults['title']!r}"
    assert defaults["event_date"] == "''", f"event_date DEFAULT: {defaults['event_date']!r}"


def test_row_counts_preserved(tmp_path: Path) -> None:
    conn = _db_with_broken_schema(tmp_path)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 1


def test_row_values_preserved(tmp_path: Path) -> None:
    conn = _db_with_broken_schema(tmp_path)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    row = conn.execute("SELECT title, event_date FROM calendar_events WHERE id=1").fetchone()
    assert row[0] == "stand-up"
    assert row[1] == "2026-04-21"


def test_integrity_check_passes(tmp_path: Path) -> None:
    conn = _db_with_broken_schema(tmp_path)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_no_op_when_already_compliant(tmp_path: Path) -> None:
    conn = _db_already_compliant(tmp_path)
    before = conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]

    stderr_buf = io.StringIO()
    conn.execute("BEGIN")
    with contextlib.redirect_stderr(stderr_buf):
        up(conn)
    conn.execute("COMMIT")

    assert "nothing to do" in stderr_buf.getvalue(), f"got: {stderr_buf.getvalue()!r}"
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == before


def test_idempotent(tmp_path: Path) -> None:
    conn = _db_with_broken_schema(tmp_path)

    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    before = conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0]

    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == before
    defaults = _get_col_defaults(conn, "calendar_events")
    assert defaults["title"] == "''"
    assert defaults["event_date"] == "''"


def test_quoted_table_name_in_sqlite_master(tmp_path: Path) -> None:
    """sqlite_master may store CREATE TABLE "calendar_events" — regex must handle it."""
    conn = _db_with_broken_schema(tmp_path)  # already creates with quoted name

    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    defaults = _get_col_defaults(conn, "calendar_events")
    assert defaults["title"] == "''"
    assert defaults["event_date"] == "''"
    assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 1
