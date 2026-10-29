#!/usr/bin/env python3
"""Restore a tracker SQLite database from a full snapshot backup."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any

from send2trash import send2trash

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.config import DATABASE_PATH
from scripts.db.schema import mark_if_holding_data


class BackupRestoreError(RuntimeError):
    """Raised when a restore request is invalid or unsafe."""


def _copy_sqlite_database(source_path: Path, destination_path: Path) -> None:
    """Copy a SQLite database, including committed WAL contents."""
    try:
        source_uri = f"file:{source_path}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(destination_path) as destination:
                source.backup(destination)
    except sqlite3.Error as err:
        raise BackupRestoreError(f"Failed to copy SQLite database: {err}") from err


def validate_backup_file(backup_path: Path) -> dict[str, Any]:
    """Validate that ``backup_path`` is a readable tracker SQLite backup."""
    if not backup_path.exists():
        raise BackupRestoreError(f"Backup file does not exist: {backup_path}")
    if not backup_path.is_file():
        raise BackupRestoreError(f"Backup path is not a file: {backup_path}")

    try:
        with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA schema_version")
            cursor.fetchone()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in cursor.fetchall()}
    except sqlite3.Error as err:
        raise BackupRestoreError(f"Backup file is not a readable SQLite database: {err}") from err

    required = {"tasks", "projects"}
    if not required.issubset(tables):
        missing = ", ".join(sorted(required - tables))
        raise BackupRestoreError(
            f"Backup is missing expected tracker tables: {missing}"
        )

    return {
        "path": str(backup_path),
        "size_bytes": backup_path.stat().st_size,
        "tables": sorted(tables),
    }


def restore_database(
    backup_path: Path,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Restore ``db_path`` from ``backup_path`` and keep a pre-restore copy."""
    backup_path = backup_path.expanduser().resolve()
    db_path = (db_path or DATABASE_PATH).expanduser().resolve()
    backup_dir = (backup_dir or (db_path.parent / "backups")).expanduser().resolve()

    if backup_path == db_path:
        raise BackupRestoreError("Refusing to restore from the live database path itself.")

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_info = validate_backup_file(backup_path)

    pre_restore_backup = None
    if db_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        pre_restore_backup = backup_dir / f"tracker_pre_restore_{timestamp}.db"
        _copy_sqlite_database(db_path, pre_restore_backup)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(db_path.parent), prefix="tracker-restore-", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        _copy_sqlite_database(backup_path, tmp_path)
        validate_backup_file(tmp_path)
        os.replace(tmp_path, db_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    # Stale WAL/SHM files from the previous live DB would be replayed into the
    # restored one; they go to the Trash, recoverable, like every user file.
    removed_sidecars: list[str] = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            send2trash(str(sidecar))
            removed_sidecars.append(str(sidecar))

    # A restore replaces the database on purpose; the fresh-database guard
    # now protects the restored data.
    with closing(sqlite3.connect(db_path)) as conn:
        mark_if_holding_data(conn, db_path)

    return {
        "restored_path": str(db_path),
        "source_backup": backup_info["path"],
        "source_size_bytes": backup_info["size_bytes"],
        "pre_restore_backup": str(pre_restore_backup) if pre_restore_backup else None,
        "removed_sidecars": removed_sidecars,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore tracker.db from a full backup snapshot")
    parser.add_argument("backup_file", help="Path to a tracker_*.db backup file")
    parser.add_argument(
        "--db-path",
        default=str(DATABASE_PATH),
        help=f"Path to the live tracker database (default: {DATABASE_PATH})",
    )
    args = parser.parse_args()

    result = restore_database(Path(args.backup_file), db_path=Path(args.db_path))
    print(f"Restored: {result['restored_path']}")
    if result["pre_restore_backup"]:
        print(f"Pre-restore backup: {result['pre_restore_backup']}")
    if result["removed_sidecars"]:
        print("Removed sidecars:")
        for sidecar in result["removed_sidecars"]:
            print(f"  - {sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
