"""Tests for migration 009 — task_display_ids LOCAL_ONLY table."""

from __future__ import annotations

import importlib.util
import io
import contextlib
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))

_MIGRATION_PATH = REPO / "scripts" / "db" / "migrations" / "009_task_display_ids.py"
_spec = importlib.util.spec_from_file_location("_m009", _MIGRATION_PATH)
assert _spec and _spec.loader
_m009 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m009)  # type: ignore[union-attr]
up = _m009.up

_SNOWFLAKE = 10 ** 12


def _make_db(tmp_path: Path, task_ids: list[int]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "tracker.db"))
    conn.execute("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Backlog',
            project_id TEXT NOT NULL DEFAULT 'test',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.executemany("INSERT INTO tasks (id) VALUES (?)", [(tid,) for tid in task_ids])
    conn.commit()
    return conn


def test_creates_table(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, [])
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "task_display_ids" in tables


def test_legacy_tasks_get_identity_mapping(tmp_path: Path) -> None:
    legacy_ids = [1000, 2000, 5000, 6029]
    conn = _make_db(tmp_path, legacy_ids)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    mapping = {r[0]: r[1] for r in conn.execute("SELECT task_id, display_id FROM task_display_ids").fetchall()}
    for tid in legacy_ids:
        assert mapping[tid] == tid, f"legacy id {tid} should map to itself, got {mapping[tid]}"


def test_snowflake_tasks_get_sequential_display_ids(tmp_path: Path) -> None:
    snowflake_ids = [_SNOWFLAKE + 1, _SNOWFLAKE + 1000, _SNOWFLAKE + 9999]
    conn = _make_db(tmp_path, snowflake_ids)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    mapping = dict(conn.execute("SELECT task_id, display_id FROM task_display_ids ORDER BY task_id").fetchall())
    display_ids = sorted(mapping.values())
    assert display_ids == list(range(1, len(snowflake_ids) + 1))


def test_mixed_tasks_snowflake_continues_from_max_legacy(tmp_path: Path) -> None:
    legacy_ids = [100, 200, 300]
    snowflake_ids = [_SNOWFLAKE + 1, _SNOWFLAKE + 2]
    conn = _make_db(tmp_path, legacy_ids + snowflake_ids)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    display_ids = sorted(
        r[0] for r in conn.execute("SELECT display_id FROM task_display_ids WHERE task_id >= ?", (_SNOWFLAKE,)).fetchall()
    )
    assert display_ids[0] == 301, f"first Snowflake display_id should be 301, got {display_ids[0]}"
    assert display_ids[1] == 302


def test_idempotent(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, [1, 2, 3])
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    count_before = conn.execute("SELECT COUNT(*) FROM task_display_ids").fetchone()[0]

    stderr_buf = io.StringIO()
    conn.execute("BEGIN")
    with contextlib.redirect_stderr(stderr_buf):
        up(conn)
    conn.execute("COMMIT")

    count_after = conn.execute("SELECT COUNT(*) FROM task_display_ids").fetchone()[0]
    assert count_before == count_after
    assert "already populated" in stderr_buf.getvalue()


def test_empty_tasks_table(tmp_path: Path) -> None:
    conn = _make_db(tmp_path, [])
    stderr_buf = io.StringIO()
    conn.execute("BEGIN")
    with contextlib.redirect_stderr(stderr_buf):
        up(conn)
    conn.execute("COMMIT")

    assert "no tasks" in stderr_buf.getvalue()
    assert conn.execute("SELECT COUNT(*) FROM task_display_ids").fetchone()[0] == 0


def test_display_ids_are_unique(tmp_path: Path) -> None:
    ids = [1, 2, 3, _SNOWFLAKE + 1, _SNOWFLAKE + 2]
    conn = _make_db(tmp_path, ids)
    conn.execute("BEGIN")
    up(conn)
    conn.execute("COMMIT")

    all_display = [r[0] for r in conn.execute("SELECT display_id FROM task_display_ids").fetchall()]
    assert len(all_display) == len(set(all_display)), "display_ids must be unique"


# -----------------------------------------------------------------------
# DatabaseManager integration tests
# -----------------------------------------------------------------------

import pytest

from db.manager import DatabaseManager


@pytest.fixture
def db_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    monkeypatch.setenv("PT_DB_PATH", str(tmp_path / "tracker.db"))
    monkeypatch.setenv("PT_ALLOW_FRESH_DB", "1")
    db = DatabaseManager()
    db.add_project("test", "Test Project", "/tmp/test", "active")
    return db


def test_get_task_display_id_returns_display_id(db_manager: DatabaseManager) -> None:
    task = db_manager.add_task("test task display id", project_id="test")
    task_id = task["id"]
    display_id = db_manager.get_task_display_id(task_id)
    assert display_id is not None, "display_id should be set after add_task"
    assert isinstance(display_id, int)


def test_get_task_display_id_returns_none_for_unknown(db_manager: DatabaseManager) -> None:
    assert db_manager.get_task_display_id(999999999999999) is None


def test_resolve_task_id_via_display_id(db_manager: DatabaseManager) -> None:
    task = db_manager.add_task("resolve via display_id", project_id="test")
    task_id = task["id"]
    display_id = db_manager.get_task_display_id(task_id)
    assert display_id is not None
    resolved = db_manager.resolve_task_id(display_id)
    assert resolved == task_id, f"resolve_task_id({display_id}) should return {task_id}, got {resolved}"


def test_resolve_task_id_via_snowflake_pk(db_manager: DatabaseManager) -> None:
    task = db_manager.add_task("resolve via snowflake pk", project_id="test")
    task_id = task["id"]
    resolved = db_manager.resolve_task_id(task_id)
    assert resolved == task_id


def test_resolve_task_id_unknown_returns_none(db_manager: DatabaseManager) -> None:
    assert db_manager.resolve_task_id(888888888) is None


def test_get_task_display_id_map_batch(db_manager: DatabaseManager) -> None:
    t1 = db_manager.add_task("task 1", project_id="test")
    t2 = db_manager.add_task("task 2", project_id="test")
    ids = [t1["id"], t2["id"]]
    mapping = db_manager.get_task_display_id_map(ids)
    assert set(mapping.keys()) == set(ids)
    assert all(isinstance(v, int) for v in mapping.values())


def test_get_task_display_id_map_empty(db_manager: DatabaseManager) -> None:
    assert db_manager.get_task_display_id_map([]) == {}
