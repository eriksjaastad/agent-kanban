from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from scripts.db.backend_restore_db import BackupRestoreError, restore_database, validate_backup_file


@pytest.fixture(autouse=True)
def trashed(tmp_path, monkeypatch) -> list[str]:
    """Every restore here sends sidecars to tmp_path/Trash, never the real Trash; yields what was sent."""
    trash = tmp_path / "Trash"
    trash.mkdir()
    sent: list[str] = []
    monkeypatch.setattr("scripts.db.backend_restore_db.send2trash",
                        lambda path: (sent.append(path), Path(path).rename(trash / Path(path).name)))
    return sent


def _make_tracker_db(path: Path, task_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
        cursor.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, text TEXT)")
        cursor.execute("INSERT INTO projects (id, name) VALUES ('demo', 'Demo')")
        cursor.execute("INSERT INTO tasks (id, text) VALUES (1, ?)", (task_text,))
        conn.commit()


def _read_task_text(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT text FROM tasks WHERE id = 1")
        return cursor.fetchone()[0]


def test_restore_database_replaces_live_db_and_keeps_pre_restore_copy(tmp_path):
    live_db = tmp_path / "live" / "tracker.db"
    backup_db = tmp_path / "backups" / "tracker_20260423_141355.db"
    pre_restore_dir = tmp_path / "pre-restore"

    _make_tracker_db(live_db, "live task")
    _make_tracker_db(backup_db, "restored task")

    result = restore_database(backup_db, db_path=live_db, backup_dir=pre_restore_dir)

    assert _read_task_text(live_db) == "restored task"
    assert (live_db.parent / ".db-fingerprint").exists()  # #8243: the guard protects restored data
    assert result["pre_restore_backup"] is not None
    assert Path(result["pre_restore_backup"]).exists()
    assert _read_task_text(Path(result["pre_restore_backup"])) == "live task"


def test_restore_database_pre_restore_copy_includes_committed_wal_rows(tmp_path):
    live_db = tmp_path / "live" / "tracker.db"
    backup_db = tmp_path / "backups" / "tracker_20260423_141355.db"

    _make_tracker_db(backup_db, "restored task")
    live_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(live_db)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA wal_autocheckpoint=0")
        cursor.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
        cursor.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, text TEXT)")
        cursor.execute("INSERT INTO projects (id, name) VALUES ('demo', 'Demo')")
        cursor.execute("INSERT INTO tasks (id, text) VALUES (1, 'checkpointed')")
        conn.commit()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cursor.execute("INSERT INTO tasks (id, text) VALUES (2, 'wal-only')")
        conn.commit()

        result = restore_database(backup_db, db_path=live_db, backup_dir=tmp_path / "pre-restore")
    finally:
        conn.close()

    with sqlite3.connect(result["pre_restore_backup"]) as pre_restore:
        rows = pre_restore.execute("SELECT id, text FROM tasks ORDER BY id").fetchall()
    assert rows == [(1, "checkpointed"), (2, "wal-only")]


def test_restore_database_trashes_stale_wal_and_shm_sidecars(tmp_path, trashed):
    live_db = tmp_path / "live" / "tracker.db"
    backup_db = tmp_path / "backups" / "tracker_20260423_141355.db"

    _make_tracker_db(live_db, "live task")
    _make_tracker_db(backup_db, "restored task")
    (tmp_path / "live" / "tracker.db-wal").write_text("stale wal")
    (tmp_path / "live" / "tracker.db-shm").write_text("stale shm")

    result = restore_database(backup_db, db_path=live_db, backup_dir=tmp_path / "pre-restore")

    assert _read_task_text(live_db) == "restored task"
    assert trashed == result["removed_sidecars"]
    assert not (tmp_path / "live" / "tracker.db-wal").exists()
    assert not (tmp_path / "live" / "tracker.db-shm").exists()
    assert len(result["removed_sidecars"]) == 2


def test_validate_backup_file_rejects_non_sqlite_payload(tmp_path):
    invalid = tmp_path / "not-a-db.txt"
    invalid.write_text("definitely not sqlite")

    with pytest.raises(BackupRestoreError):
        validate_backup_file(invalid)
