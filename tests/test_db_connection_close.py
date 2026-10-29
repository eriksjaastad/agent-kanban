"""`DatabaseManager._get_conn` returns a usable connection and releases it (#6482).

The dashboard opens a connection per request; leaked descriptors eventually hit
the 256 soft limit and every query fails with "unable to open database file"
while the process stays up. The leak was cr-sqlite-specific and is gone with
the extension, but this guards the invariant for any future change.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.backend_manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402


def _open_descriptors() -> int:
    """Open descriptors in this process, counted in-process (no subprocess).

    Listing /dev/fd briefly holds one descriptor of its own; callers compare two
    counts, so that one cancels out.
    """
    return len(os.listdir("/dev/fd"))


def test_connection_still_usable_and_closed_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    db = DatabaseManager(db_path)

    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="no /dev/fd on this platform")
def test_repeated_connections_do_not_leak_descriptors(tmp_path: Path) -> None:
    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    db = DatabaseManager(db_path)

    with db._get_conn() as conn:
        conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    baseline = _open_descriptors()

    for _ in range(60):
        with db._get_conn() as conn:
            conn.execute("SELECT COUNT(*) FROM tasks").fetchone()

    # A leaked connection holds at least the database file; 60 of them would
    # push the delta far past this slack.
    assert _open_descriptors() - baseline <= 4
