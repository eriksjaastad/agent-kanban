"""Tests for the `pt retire-project` CLI command (#5851)."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.schema import create_database


@pytest.fixture
def fabricated_project(monkeypatch):
    """Build a disposable project with a real on-disk dir and fully populated DB."""
    with tempfile.TemporaryDirectory() as tmp:
        projects_root = Path(tmp)
        project_dir = projects_root / "humpty-dumpty"
        project_dir.mkdir()
        (project_dir / "README.md").write_text("# humpty-dumpty\n")

        db_path = projects_root / "tracker.db"
        create_database(db_path)

        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        monkeypatch.setenv("PROJECTS_ROOT", str(projects_root))

        import scripts.pt as pt_module
        monkeypatch.setattr(pt_module, "PROJECTS_BASE_DIR", projects_root)

        from db.manager import DatabaseManager as DM
        db = DM()
        db.add_project(
            project_id="humpty-dumpty",
            name="humpty-dumpty",
            path=str(project_dir),
            status="active",
        )

        db.add_task("First shell crack", "humpty-dumpty", priority="Medium")
        db.add_task("Put him back together", "humpty-dumpty", priority="Medium")

        from db.calendar_manager import CalendarManager
        CalendarManager().add_cron_job("humpty-dumpty", "0 * * * *", "echo wall", "hourly wall check")
        db.add_ai_agent("humpty-dumpty", "king-horse", "attempts reconstruction")

        yield {
            "projects_root": projects_root,
            "project_dir": project_dir,
            "db_path": db_path,
            "db": db,
        }


def _get_cli():
    from scripts.pt import cli as pt_cli
    return pt_cli


def test_dry_run_reports_but_changes_nothing(fabricated_project):
    cli = _get_cli()
    runner = CliRunner()
    with patch("send2trash.send2trash") as trash_mock:
        result = runner.invoke(cli, ["retire-project", "humpty-dumpty"])

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "Tasks:    2" in result.output
    assert "Crons:    1" in result.output
    assert "Agents:   1" in result.output
    assert "Dry run complete" in result.output
    trash_mock.assert_not_called()

    # Verify DB and filesystem untouched
    db = fabricated_project["db"]
    assert db.get_project("humpty-dumpty") is not None
    assert len(db.get_tasks(project_id="humpty-dumpty")) == 2
    assert fabricated_project["project_dir"].exists()


def test_execute_trashes_dir_and_cascades_db(fabricated_project):
    cli = _get_cli()
    runner = CliRunner()
    with patch("send2trash.send2trash") as trash_mock:
        result = runner.invoke(
            cli, ["retire-project", "humpty-dumpty", "--execute", "-y"]
        )

    assert result.exit_code == 0, result.output
    assert "EXECUTE" in result.output
    assert "Retired" in result.output
    trash_mock.assert_called_once()
    called_with = trash_mock.call_args[0][0]
    assert "humpty-dumpty" in str(called_with)

    db = fabricated_project["db"]
    assert db.get_project("humpty-dumpty") is None
    assert len(db.get_tasks(project_id="humpty-dumpty")) == 0
    assert len(db.get_cron_jobs("humpty-dumpty")) == 0
    assert len(db.get_ai_agents("humpty-dumpty")) == 0


def test_keep_files_skips_trash_but_cleans_db(fabricated_project):
    cli = _get_cli()
    runner = CliRunner()
    with patch("send2trash.send2trash") as trash_mock:
        result = runner.invoke(
            cli,
            ["retire-project", "humpty-dumpty", "--execute", "--keep-files", "-y"],
        )

    assert result.exit_code == 0, result.output
    trash_mock.assert_not_called()
    assert fabricated_project["project_dir"].exists()

    db = fabricated_project["db"]
    assert db.get_project("humpty-dumpty") is None
    assert len(db.get_tasks(project_id="humpty-dumpty")) == 0


def test_retire_backup_failure_leaves_files_and_rows(fabricated_project, monkeypatch):
    from db.operations import ProjectTrackerOps

    monkeypatch.setattr(ProjectTrackerOps, "verify_backup", lambda self, path: False)
    with patch("send2trash.send2trash") as trash_mock:
        result = CliRunner().invoke(
            _get_cli(), ["retire-project", "humpty-dumpty", "--execute", "-y"]
        )

    assert result.exit_code != 0
    trash_mock.assert_not_called()
    assert fabricated_project["project_dir"].exists()
    assert len(fabricated_project["db"].get_tasks(project_id="humpty-dumpty")) == 2


def test_retire_uses_preflight_snapshot_after_moving_directory(fabricated_project, monkeypatch):
    from db.operations import ProjectTrackerOps

    events = []
    original_snapshot = ProjectTrackerOps.create_recovery_snapshot
    original_verify = ProjectTrackerOps.verify_backup
    project_dir = fabricated_project['project_dir']
    trashed = fabricated_project['projects_root'] / 'synthetic-trash'

    def snapshot(self, label):
        if 'snapshot' in events:
            raise OSError('No space for a duplicate snapshot')
        events.append('snapshot')
        return original_snapshot(self, label)

    def verify(self, path):
        events.append('verified')
        return original_verify(self, path)

    def trash(path):
        assert events == ['snapshot', 'verified']
        events.append('trash')
        Path(path).rename(trashed)

    monkeypatch.setattr(ProjectTrackerOps, 'create_recovery_snapshot', snapshot)
    monkeypatch.setattr(ProjectTrackerOps, 'verify_backup', verify)
    with patch('send2trash.send2trash', side_effect=trash):
        result = CliRunner().invoke(
            _get_cli(), ['retire-project', 'humpty-dumpty', '--execute', '-y']
        )

    assert result.exit_code == 0, result.output
    assert events == ['snapshot', 'verified', 'trash']
    assert not project_dir.exists()
    assert (trashed / 'README.md').read_text() == '# humpty-dumpty\n'
    assert fabricated_project['db'].get_project('humpty-dumpty') is None
    assert fabricated_project['db'].get_tasks(project_id='humpty-dumpty') == []


@pytest.mark.parametrize('backend_fails', [False, True])
def test_prepared_operation_is_bound_and_single_use(db, monkeypatch, backend_fails):
    calls = []

    def delete(self, project_id):
        calls.append(project_id)
        if backend_fails:
            raise OSError('Backend failure after possible commit')
        return 'deleted'

    from db.backend_manager import DatabaseManager as CoreDatabaseManager
    monkeypatch.setattr(CoreDatabaseManager, 'delete_project', delete)
    prepared = db.prepare_operation(
        'delete_project', reason='Test one-shot retirement', project_id='chosen'
    )
    assert calls == []
    if backend_fails:
        with pytest.raises(OSError, match='possible commit'):
            prepared()
    else:
        assert prepared() == 'deleted'
    with pytest.raises(RuntimeError, match='already consumed'):
        prepared()
    assert calls == ['chosen']


def test_retire_trash_failure_preserves_rows(fabricated_project):
    with patch("send2trash.send2trash", side_effect=OSError("trash unavailable")):
        result = CliRunner().invoke(
            _get_cli(), ["retire-project", "humpty-dumpty", "--execute", "-y"]
        )

    assert result.exit_code != 0
    assert "Aborting before DB delete" in result.output
    assert fabricated_project["project_dir"].exists()
    assert len(fabricated_project["db"].get_tasks(project_id="humpty-dumpty")) == 2


def test_unknown_project_exits_nonzero_and_touches_nothing(fabricated_project):
    cli = _get_cli()
    runner = CliRunner()
    with patch("send2trash.send2trash") as trash_mock:
        result = runner.invoke(
            cli, ["retire-project", "nonexistent-project", "--execute", "-y"]
        )

    assert result.exit_code != 0
    assert "not found" in result.output.lower()
    trash_mock.assert_not_called()

    # Humpty is unharmed
    db = fabricated_project["db"]
    assert db.get_project("humpty-dumpty") is not None
    assert fabricated_project["project_dir"].exists()


def test_stale_reference_scan_finds_hits_in_sibling_project(fabricated_project):
    sibling = fabricated_project["projects_root"] / "other-project"
    sibling.mkdir()
    (sibling / "notes.md").write_text("Depends on humpty-dumpty for reconstruction.\n")

    cli = _get_cli()
    runner = CliRunner()
    result = runner.invoke(cli, ["retire-project", "humpty-dumpty"])

    assert result.exit_code == 0
    assert "notes.md" in result.output
    assert "humpty-dumpty" in result.output.lower()
