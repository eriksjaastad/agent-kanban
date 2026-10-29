"""Migration 021 turns escaped task text back into what was typed (#8246)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.migration_runner import discover_migrations  # noqa: E402

MIGRATIONS_DIR = Path(__file__).parent.parent / "scripts" / "db" / "migrations"

ROWS = {
    # stored by the old code -> expected after the migration
    "Owner&#x27;s &lt;b&gt;Q&amp;A&lt;/b&gt; &quot;x&quot;": "Owner's <b>Q&A</b> \"x\"",
    # raw rows (pre-escaping, or written by the new code) stay as they are,
    # including one plain html.unescape would corrupt (&not -> ¬)
    "foo&notes": "foo&notes",
    "a & b": "a & b",
    "Owner's": "Owner's",
    "plain": "plain",
    # escaped twice by re-saves: one layer comes off
    "Fish &amp;amp; chips": "Fish &amp; chips",
}


def test_unescapes_only_rows_the_old_code_escaped() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, text TEXT, updated_at TEXT)")
    conn.executemany(
        "INSERT INTO tasks (text, updated_at) VALUES (?, 'stamp')", [(t,) for t in ROWS]
    )
    migration = next(m for m in discover_migrations(MIGRATIONS_DIR) if m.version == 21)

    migration.up(conn)

    stored = [row[0] for row in conn.execute("SELECT text FROM tasks ORDER BY id")]
    assert stored == list(ROWS.values())
    assert {row[0] for row in conn.execute("SELECT updated_at FROM tasks")} == {"stamp"}
