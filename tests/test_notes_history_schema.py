"""task_notes_history: columns, defaults and index (#7642)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.schema import schema_statements  # noqa: E402


def _schema(conn: sqlite3.Connection) -> None:
    """Build the current schema (schema.sql) on ``conn``."""
    for statement in schema_statements():
        conn.execute(statement)


def test_notes_history_columns_defaults_and_index(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "notes.db")
    _schema(conn)

    names = [row[1] for row in conn.execute("PRAGMA table_info(task_notes_history)")]
    assert names == [
        "id", "task_id", "project_id", "old_notes", "new_notes", "source", "timestamp",
    ]
    index_names = {
        row[1] for row in conn.execute("PRAGMA index_list(task_notes_history)").fetchall()
    }
    assert "idx_task_notes_history_task_timestamp" in index_names

    conn.execute(
        "INSERT INTO task_notes_history (task_id, project_id, timestamp) "
        "VALUES (1, 'project-tracker', '2026-09-26T00:00:00')"
    )
    assert conn.execute("SELECT source FROM task_notes_history").fetchone()[0] == "unknown"
    conn.close()


