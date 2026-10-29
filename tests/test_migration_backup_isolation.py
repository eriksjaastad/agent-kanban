"""Test that migrations respect PT_EXTERNAL_BACKUP_DIR for test isolation.

Ensures migrations 003-008 do not write backups into the real disaster-recovery
directory (~/.project-tracker/backups) when PT_EXTERNAL_BACKUP_DIR is set.

Card #7338
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Path setup
REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _test_backup_function(migration_num: str, tmp_path: Path) -> None:
    """Test just the _backup function to verify isolation."""
    import importlib.util
    
    migration_path = REPO / "scripts" / "db" / "migrations" / f"{migration_num}.py"
    spec = importlib.util.spec_from_file_location(f"_m{migration_num}", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    
    # Create a minimal database
    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
    conn.commit()
    
    # Call the _backup function
    if "007" in migration_num or "008" in migration_num:
        # These take a migration_num parameter
        module._backup(conn, migration_num.split('_')[0])
    else:
        module._backup(conn)
    
    conn.close()


@pytest.mark.parametrize("migration_num", [
    "003_crr_pk_not_null",
    "004_crr_notnull_defaults",
    "005_crr_drop_fk_constraints",
    "006_crr_drop_unique_constraints",
    "007_crr_fix_missing_defaults",
    "008_crr_fix_calendar_events_title_date",
])
def test_migration_backup_respects_external_backup_dir(migration_num: str, tmp_path: Path) -> None:
    """Verify migration _backup functions write to PT_EXTERNAL_BACKUP_DIR, not hardcoded path."""
    # Real disaster-recovery directory that migrations must NOT touch during tests
    real_backup_dir = Path.home() / ".project-tracker" / "backups"
    
    # Conftest already set PT_EXTERNAL_BACKUP_DIR to a temp directory,
    # but verify it's not the real one
    env_external_backup = os.getenv("PT_EXTERNAL_BACKUP_DIR")
    assert env_external_backup is not None, "conftest should set PT_EXTERNAL_BACKUP_DIR"
    assert Path(env_external_backup) != real_backup_dir, \
        "PT_EXTERNAL_BACKUP_DIR must not be the real disaster-recovery directory in tests"
    
    # Track real backup dir contents before backup
    real_backup_files_before = set(real_backup_dir.iterdir()) if real_backup_dir.exists() else set()
    
    # Track test backup dir contents before this specific invocation
    test_external_backup_dir = Path(env_external_backup)
    test_backup_files_before = set(test_external_backup_dir.iterdir()) if test_external_backup_dir.exists() else set()
    
    # Run the backup function
    _test_backup_function(migration_num, tmp_path)
    
    # Verify real backup dir was NOT touched
    real_backup_files_after = set(real_backup_dir.iterdir()) if real_backup_dir.exists() else set()
    new_files_in_real_backup_dir = real_backup_files_after - real_backup_files_before
    
    assert len(new_files_in_real_backup_dir) == 0, (
        f"Migration {migration_num} wrote {len(new_files_in_real_backup_dir)} backup file(s) "
        f"to the real disaster-recovery directory {real_backup_dir}, "
        f"violating test isolation. Files: {new_files_in_real_backup_dir}"
    )
    
    # Verify backup WAS created in the test-scoped directory by THIS invocation
    test_backup_files_after = set(test_external_backup_dir.iterdir()) if test_external_backup_dir.exists() else set()
    new_files_in_test_backup_dir = test_backup_files_after - test_backup_files_before
    
    assert len(new_files_in_test_backup_dir) >= 1, (
        f"Migration {migration_num} should have written at least one backup to "
        f"PT_EXTERNAL_BACKUP_DIR={test_external_backup_dir} during this invocation, but none found. "
        f"Files before: {len(test_backup_files_before)}, after: {len(test_backup_files_after)}"
    )
    
    # Verify the backup filename matches the expected pattern
    migration_prefix = migration_num.split('_')[0]
    new_backups_with_correct_prefix = [
        f for f in new_files_in_test_backup_dir 
        if f.name.startswith(f"pre_{migration_prefix}_")
    ]
    
    assert len(new_backups_with_correct_prefix) >= 1, (
        f"Migration {migration_num} created backup(s) in test directory, "
        f"but none matched expected prefix 'pre_{migration_prefix}_'. "
        f"Created: {[f.name for f in new_files_in_test_backup_dir]}"
    )
