"""Recovery behavior after removing the mediation service."""
import sqlite3
import subprocess
from pathlib import Path

import pytest

from db.backend_manager import DatabaseManager as CoreDatabaseManager
from db.manager import DatabaseManager
from db.calendar_manager import CalendarManager


@pytest.mark.parametrize('op', ['delete_project', 'delete_done_tasks', 'trim_done_tasks', 'raw_import_tasks'])
@pytest.mark.parametrize('explicit', [False, True])
def test_destructive_backend_calls_verify_snapshot_before_mutation(db, monkeypatch, op, explicit):
    events = []
    monkeypatch.setattr(db, 'create_recovery_snapshot', lambda **kw: events.append('snapshot') or Path('synthetic.db'))
    monkeypatch.setattr(db, 'verify_backup', lambda path: events.append('verified') or True)
    monkeypatch.setattr(CoreDatabaseManager, op, lambda self, **kw: events.append(('mutation', kw)) or 7)
    if explicit:
        result = db.authorize(op, reason='Synthetic explicit operation', marker='preserved')
    else:
        result = getattr(db, op)(marker='preserved')
    assert result == 7
    assert events == ['snapshot', 'verified', ('mutation', {'marker': 'preserved'})]


@pytest.mark.parametrize('op', ['delete_project', 'delete_done_tasks', 'trim_done_tasks', 'raw_import_tasks', 'backup_restore', 'migrations_apply'])
def test_direct_destructive_call_stops_on_invalid_backup(db, monkeypatch, op):
    called = []
    monkeypatch.setattr(db, 'verify_backup', lambda path: False)
    target, name = (CoreDatabaseManager, op) if op in db._DESTRUCTIVE_BACKEND_OPS else (db, f'_{op}')
    monkeypatch.setattr(target, name, lambda *a, **kw: called.append(op))
    with pytest.raises(RuntimeError, match='Recovery snapshot failed verification'):
        getattr(db, op)(*(() if op == 'migrations_apply' else ('synthetic',)))
    assert called == []


@pytest.mark.parametrize('op,params', [('backup_restore', {'name': 'snapshot.db'}), ('migrations_apply', {})])
@pytest.mark.parametrize('explicit', [False, True])
def test_local_destructive_entry_paths_take_one_snapshot(db, monkeypatch, op, params, explicit):
    events = []
    monkeypatch.setattr(db, 'create_recovery_snapshot', lambda **kw: events.append('snapshot') or Path('synthetic.db'))
    monkeypatch.setattr(db, 'verify_backup', lambda path: events.append('verified') or True)
    monkeypatch.setattr(db, f'_{op}', lambda *a, **kw: events.append('mutation') or {'ok': True})
    if explicit:
        result = db.authorize(op, reason='Synthetic explicit operation', **params)
    else:
        result = getattr(db, op)(**params)
    assert result == {'ok': True}
    assert events == ['snapshot', 'verified', 'mutation']


def test_direct_import_snapshot_preserves_replaced_row(db, tmp_path):
    db.add_project('import', 'Import', str(tmp_path), 'active')
    original = db.add_task('Before import', 'import')
    replacement = {**db.get_task(original['id']), 'text': 'After import'}
    assert db.raw_import_tasks([replacement]) == 1
    assert db.get_task(original['id'])['text'] == 'After import'
    backups = db.backup_list()
    assert len(backups) == 1
    saved = Path(backups[0]['location']) / backups[0]['name']
    with sqlite3.connect(saved) as conn:
        assert conn.execute('SELECT text FROM tasks WHERE id = ?', (original['id'],)).fetchone() == ('Before import',)


def test_refresh_preserves_stale_project_if_recovery_backup_fails(db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from dashboard import app

    missing = tmp_path / 'missing-project'
    db.add_project('stale', 'Stale', str(missing), 'active')
    task = db.add_task('Preserve on failed backup', 'stale')
    monkeypatch.setattr(app, 'DatabaseManager', lambda: db)
    monkeypatch.setattr(app, 'PROJECTS_BASE_DIR', tmp_path)
    monkeypatch.setattr(app, 'discover_projects', lambda: [])
    monkeypatch.setattr(db, 'verify_backup', lambda path: False)
    result = TestClient(app.app).post('/api/refresh')
    assert result.status_code == 500
    assert 'Recovery snapshot failed verification' in result.json()['message']
    assert db.get_project('stale')['name'] == 'Stale'
    assert db.get_task(task['id'])['text'] == 'Preserve on failed backup'


def test_local_board_calendar_and_restore_work_without_service(db, monkeypatch, tmp_path):
    db.add_project('recovery', 'Recovery', str(tmp_path), 'active')
    first = db.add_task('Preserve this card', 'recovery')
    calendar = CalendarManager(db.db_path)
    event = calendar.add_event('Recovery drill', '2026-09-22', project_id='recovery')
    created = db.backup_create()
    assert created['verified'] is True
    backup_name = Path(created['path']).name
    assert {row['name'] for row in db.backup_list()} == {backup_name}
    later = db.add_task('After snapshot', 'recovery')
    marker = db.db_path.parent / '.db-fingerprint'
    marker.rename(tmp_path / 'set-aside-marker')  # #8243: the restore itself must leave one
    existing_reader = sqlite3.connect(db.db_path)
    try:
        assert existing_reader.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 2
        db.authorize('backup_restore', reason='Isolated recovery drill', name=backup_name)
        assert existing_reader.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    finally:
        existing_reader.close()
    assert marker.exists()
    restored = DatabaseManager(db.db_path)
    assert restored.get_task(first['id'])['text'] == 'Preserve this card'
    assert restored.get_task(later['id']) is None
    assert CalendarManager(db.db_path).get_event(event)['title'] == 'Recovery drill'
    assert len(restored.backup_list()) == 2


def test_rclone_uploads_snapshot_and_preserves_local_copy_on_failure(db, monkeypatch, tmp_path):
    db.add_project('offsite', 'Offsite', str(tmp_path), 'active')
    db.add_task('Offsite card', 'offsite')
    created = db.backup_create()
    config = tmp_path / 'rclone.conf'
    config.write_text('[synthetic]\ntype = local\n')
    monkeypatch.setenv('RCLONE_CONFIG', str(config))
    monkeypatch.setenv('PT_BACKUP_RCLONE_DEST', 'synthetic:recovery')
    monkeypatch.setattr('shutil.which', lambda name: '/synthetic/rclone')
    calls = []

    def upload(command, **kwargs):
        calls.append((command, kwargs))
        assert Path(command[2]) != db.db_path
        with sqlite3.connect(Path(command[2]).resolve().as_uri() + '?mode=ro', uri=True) as uploaded:
            assert uploaded.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
        return subprocess.CompletedProcess(command, 0, '', '')

    monkeypatch.setattr(subprocess, 'run', upload)
    result = db.backup_offsite_copy(Path(created['path']).name)
    assert result['dest'].startswith('synthetic:recovery/tracker_')
    assert calls[0][0][1] == 'copyto'
    assert calls[0][1]['timeout'] == 300
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw:
                        subprocess.CompletedProcess(command, 3, '', 'synthetic network failure'))
    with pytest.raises(RuntimeError, match='synthetic network failure'):
        db.backup_offsite_copy(Path(created['path']).name)
    assert db.verify_backup(created['path'])


def test_scheduled_settings_match_manual_upload_and_status(db, monkeypatch, tmp_path):
    import plistlib
    from scripts.discovery.backup_reader import get_backup_status, append_cloud_copy_log

    config = tmp_path / 'scheduled-rclone.conf'
    config.write_text('[scheduled]\ntype = local\n')
    external = tmp_path / 'scheduled-backups'
    plist = tmp_path / 'backup.plist'
    plist.write_bytes(plistlib.dumps({'EnvironmentVariables': {
        'PT_BACKUP_RCLONE_DEST': 'scheduled:recovery',
        'RCLONE_CONFIG': str(config),
        'PT_EXTERNAL_BACKUP_DIR': str(external),
    }}))
    monkeypatch.setenv('PT_BACKUP_LAUNCH_AGENT_PATH', str(plist))
    monkeypatch.setenv('PT_BACKUP_LOG_PATH', str(tmp_path / 'backup.log'))
    for key in ('PT_BACKUP_RCLONE_DEST', 'RCLONE_CONFIG', 'RCLONE_CONFIG_PATH',
                'PT_EXTERNAL_BACKUP_DIR', 'PT_FULL_BACKUP_DIR'):
        monkeypatch.delenv(key, raising=False)
    local = DatabaseManager(db.db_path)
    assert local.entry.external_backup_dir == external
    created = local.backup_create()
    monkeypatch.setattr('shutil.which', lambda name: '/synthetic/rclone')
    calls = []
    def upload(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', upload)
    result = local.backup_offsite_copy(Path(created['path']).name)
    assert calls[0][3] == result['dest']
    assert calls[0][5] == str(config)
    assert result['dest'].startswith('scheduled:recovery/')
    append_cloud_copy_log(ok=True, detail=result['dest'])
    status = get_backup_status()
    assert status['backup_dir'] == str(external)
    assert status['status'] == 'healthy'
    assert status['local_full']['count'] == 1


def test_backup_settings_process_precedence_and_legacy_aliases(monkeypatch, tmp_path):
    import plistlib
    from scripts.backup_config import external_backup_dir, rclone_config_path, rclone_destination
    plist = tmp_path / 'backup.plist'
    plist.write_bytes(plistlib.dumps({'EnvironmentVariables': {
        'PT_BACKUP_RCLONE_DEST': 'scheduled:old',
        'RCLONE_CONFIG': str(tmp_path / 'scheduled.conf'),
        'PT_EXTERNAL_BACKUP_DIR': str(tmp_path / 'scheduled'),
    }}))
    monkeypatch.setenv('PT_BACKUP_LAUNCH_AGENT_PATH', str(plist))
    monkeypatch.delenv('PT_EXTERNAL_BACKUP_DIR', raising=False)
    monkeypatch.delenv('RCLONE_CONFIG', raising=False)
    monkeypatch.setenv('PT_FULL_BACKUP_DIR', str(tmp_path / 'legacy'))
    monkeypatch.setenv('RCLONE_CONFIG_PATH', str(tmp_path / 'legacy.conf'))
    monkeypatch.setenv('PT_BACKUP_RCLONE_DEST', 'manual:new')
    assert external_backup_dir() == tmp_path / 'legacy'
    assert rclone_config_path() == tmp_path / 'legacy.conf'
    assert rclone_destination() == 'manual:new'
    monkeypatch.setenv('PT_EXTERNAL_BACKUP_DIR', str(tmp_path / 'canonical'))
    monkeypatch.setenv('RCLONE_CONFIG', str(tmp_path / 'canonical.conf'))
    assert external_backup_dir() == tmp_path / 'canonical'
    assert rclone_config_path() == tmp_path / 'canonical.conf'


@pytest.mark.parametrize('name', ['../tracker.db', '/tmp/tracker.db'])
def test_restore_cli_rejects_paths_without_traceback(db, monkeypatch, tmp_path, name):
    from click.testing import CliRunner
    import pt
    db.add_project('keep', 'Keep', str(tmp_path), 'active')
    saved = db.add_task('Keep this row', 'keep')
    monkeypatch.setattr(pt, 'DatabaseManager', lambda: db)
    result = CliRunner().invoke(pt.cli, ['backup', 'restore', name, '--yes'])
    assert result.exit_code == 2
    assert 'pt backup restore:' in result.output
    assert isinstance(result.exception, SystemExit)
    assert db.get_task(saved['id'])['text'] == 'Keep this row'


@pytest.mark.parametrize('command', [['backup', 'create'], ['backup', 'restore', 'tracker_saved.db', '--yes']])
def test_backup_cli_reports_verification_failure(db, monkeypatch, tmp_path, command):
    from click.testing import CliRunner
    import pt
    db.add_project('keep', 'Keep', str(tmp_path), 'active')
    saved = db.add_task('Keep this row', 'keep')
    monkeypatch.setattr(pt, 'DatabaseManager', lambda: db)
    monkeypatch.setattr(db, 'verify_backup', lambda path: False)
    result = CliRunner().invoke(pt.cli, command)
    assert result.exit_code == 2
    assert f'pt backup {command[1]}:' in result.output
    assert 'verification' in result.output.lower()
    assert isinstance(result.exception, SystemExit)
    assert db.get_task(saved['id'])['text'] == 'Keep this row'


def test_offsite_cli_reports_timeout_and_keeps_snapshot(db, monkeypatch, tmp_path):
    from click.testing import CliRunner
    import pt
    created = db.backup_create()
    config = tmp_path / 'rclone.conf'
    config.write_text('[synthetic]\ntype = local\n')
    monkeypatch.setenv('PT_BACKUP_LOG_PATH', str(tmp_path / 'backup.log'))
    monkeypatch.setenv('PT_BACKUP_RCLONE_DEST', 'synthetic:backups')
    monkeypatch.setenv('RCLONE_CONFIG', str(config))
    monkeypatch.setattr(pt, 'DatabaseManager', lambda: db)
    monkeypatch.setattr('shutil.which', lambda name: '/synthetic/rclone')
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 300)
    monkeypatch.setattr(subprocess, 'run', timeout)
    result = CliRunner().invoke(pt.cli, ['backup', 'offsite', Path(created['path']).name])
    assert result.exit_code == 2
    assert 'pt backup offsite:' in result.output
    assert isinstance(result.exception, SystemExit)
    assert '| cloud_copy | failed |' in (tmp_path / 'backup.log').read_text()
    assert db.verify_backup(created['path'])


@pytest.mark.parametrize('error_name', ['SafetyError', 'FreshDatabaseError'])
def test_handoff_json_preserves_database_safety_errors(monkeypatch, error_name):
    import json
    from click.testing import CliRunner
    import pt
    from db import schema
    def refuse():
        raise getattr(schema, error_name)('Database safety guard refused this database')
    monkeypatch.setattr(pt, 'DatabaseManager', refuse)
    result = CliRunner().invoke(pt.cli, ['handoff', 'list', '--json'])
    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload['error']['class'] == 'backend_unavailable'
    assert 'Database safety guard refused' in payload['error']['message']


def test_fresh_database_guard_outside_pytest_returns_json(db, tmp_path):
    import json
    import os
    import sys
    (db.db_path.parent / '.db-fingerprint').write_text('synthetic-expected-existing-data')
    env = dict(os.environ)
    env.pop('PT_ALLOW_FRESH_DB', None)
    env.pop('PT_TEST_MODE', None)
    env['PT_NO_BANNER'] = '1'
    env['PT_DB_PATH'] = str(db.db_path)
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/pt.py'),
               'handoff', 'list', '--json']
    # Use a clean child so schema's intentional pytest bypass cannot mask this guard.
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload['error']['class'] == 'backend_unavailable'
    assert 'UNEXPECTED FRESH DATABASE' in payload['error']['message']
    assert 'Traceback' not in result.stderr


def test_prune_keeps_the_newest_of_each_kind_and_trashes_the_rest(db, monkeypatch):
    """#8093 D3: newest 10 snapshots and newest 5 of every other kind, per directory."""
    import os
    import send2trash

    trashed = []
    monkeypatch.setattr(send2trash, "send2trash", lambda path: trashed.append(Path(path).name))
    directory = db.entry.backup_dir
    directory.mkdir(parents=True, exist_ok=True)

    def make(name, age):
        path = directory / name
        path.write_text("x")
        os.utime(path, (1_000_000 - age, 1_000_000 - age))

    for i in range(12):  # snapshots, newest first; the oldest has sidecars
        make(f"tracker_202610{i + 10:02d}_000000.db", age=i)
    make("tracker_20261010_000000.db-wal", age=0)  # a kept snapshot's sidecar stays
    make("tracker_20261021_000000.db-wal", age=11)
    make("tracker_20261021_000000.db-shm", age=11)
    for i in range(7):
        make(f"before_destructive_2026100{i + 1}T000000_1.db", age=i)
    for i in range(3):
        make(f"pre_018_crr_removal_2026100{i + 1}_000000.db", age=i)
    for i in range(6):
        make(f"tasks_delete_backup_2026010{i + 1}_000000.json", age=i)
    make("before_destructive_20261009T000000_9.db-wal", age=0)  # its database is gone
    make("crontab_before_poller_removal_20261009.txt", age=99)  # no kind: untouched
    for i in range(6):  # timestamped, but not a .db/.json backup: untouched
        make(f"notes_2026100{i + 1}_000000.txt", age=50 + i)

    db.backup_prune()

    assert sorted(trashed) == sorted([
        "tracker_20261020_000000.db",
        "tracker_20261021_000000.db",
        "tracker_20261021_000000.db-wal",
        "tracker_20261021_000000.db-shm",
        "before_destructive_20261006T000000_1.db",
        "before_destructive_20261007T000000_1.db",
        "tasks_delete_backup_20260106_000000.json",
        "before_destructive_20261009T000000_9.db-wal",
    ])


@pytest.mark.parametrize("failing", ["trash", "listing"])
def test_prune_reports_every_failure_and_leaves_files(db, monkeypatch, failing):
    """A Trash or listing failure is one reported line and exit 1, never a traceback."""
    import os
    import send2trash
    from click.testing import CliRunner
    from pt import backup_prune as backup_prune_cmd

    def refuse(*_args):
        raise PermissionError(1, "Operation not permitted | see\nnext line")

    directory = db.entry.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        path = directory / f"tracker_202609{i + 10:02d}_000000.db"
        path.write_text("x")
        os.utime(path, (1_000_000 - i, 1_000_000 - i))
    import pt

    def must_not_trash(path):
        pytest.fail(f"listing failed, yet {path} was sent to the Trash")

    # Every case stubs send2trash, so no test can reach the real Trash. The
    # listing case patches the class the CLI builds (pt may import it under
    # scripts.db rather than db).
    if failing == "trash":
        monkeypatch.setattr(send2trash, "send2trash", refuse)
    else:
        monkeypatch.setattr(send2trash, "send2trash", must_not_trash)
        monkeypatch.setattr(pt.DatabaseManager, "_backups_to_prune", refuse)

    result = CliRunner().invoke(backup_prune_cmd, [])

    assert result.exit_code == 1, result.output
    assert result.output.count("\n") == 1
    assert "left in place" in result.output and "Operation not permitted / see next line" in result.output
    assert len(list(directory.glob("tracker_*.db"))) == 12


def test_prune_dry_run_moves_nothing(db, monkeypatch):
    import os
    import send2trash

    monkeypatch.setattr(send2trash, "send2trash", lambda path: pytest.fail(f"dry run trashed {path}"))
    directory = db.entry.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        path = directory / f"tracker_202609{i + 10:02d}_000000.db"
        path.write_text("x")
        os.utime(path, (1_000_000 - i, 1_000_000 - i))

    result = db.backup_prune(dry_run=True)

    assert result["pruned"] == ["tracker_20260920_000000.db", "tracker_20260921_000000.db"]
    assert len(list(directory.glob("tracker_*.db"))) == 12


def test_prune_keeps_sidecars_with_a_database_that_cannot_move(db, monkeypatch):
    import os
    import send2trash

    trashed = []

    def trash(path):
        if path.endswith(".db"):
            raise PermissionError(1, "Operation not permitted")
        trashed.append(Path(path).name)

    monkeypatch.setattr(send2trash, "send2trash", trash)
    directory = db.entry.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(11):
        for suffix in ("", "-wal", "-shm") if i == 10 else ("",):
            path = directory / f"tracker_202609{i + 10:02d}_000000.db{suffix}"
            path.write_text("x")
            os.utime(path, (1_000_000 - i, 1_000_000 - i))

    result = db.backup_prune()

    assert trashed == []  # the oldest database stayed, so its WAL and SHM stayed too
    assert result["errors"] == ["tracker_20260920_000000.db: [Errno 1] Operation not permitted"]


def test_prune_reports_an_unreadable_backup_directory_and_prunes_the_other(db, monkeypatch):
    import os
    import send2trash

    trashed = []
    monkeypatch.setattr(send2trash, "send2trash", lambda path: trashed.append(Path(path).name))
    unreadable = db.entry.backup_dir
    unreadable.mkdir(parents=True, exist_ok=True)
    other = db.entry.external_backup_dir
    other.mkdir(parents=True, exist_ok=True)
    for i in range(11):
        path = other / f"tracker_202609{i + 10:02d}_000000.db"
        path.write_text("x")
        os.utime(path, (1_000_000 - i, 1_000_000 - i))
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self == unreadable:
            raise PermissionError(13, "Permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    result = db.backup_prune()

    assert result["errors"] and result["errors"][0].startswith(f"listing {unreadable}:")
    assert trashed == ["tracker_20260920_000000.db"]
