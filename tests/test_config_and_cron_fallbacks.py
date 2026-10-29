"""Regression tests for config and cron fallback behavior."""

import importlib
import importlib.util
import logging
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import scripts.discovery.cron_monitor as cron_monitor
import scripts.db.schema as schema
import scripts.pt as pt
from scripts.db.schema import _check_fresh_database

logger = logging.getLogger(__name__)


def test_config_uses_external_backup_dir_override(tmp_path, monkeypatch):
    projects_root = tmp_path / "projects"
    backup_dir = tmp_path / "external-backups"
    projects_root.mkdir()

    monkeypatch.setenv("PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("PT_EXTERNAL_BACKUP_DIR", str(backup_dir))

    import scripts.config as config

    reloaded = importlib.reload(config)

    assert reloaded.EXTERNAL_BACKUP_DIR == backup_dir


def test_config_defaults_external_backup_dir_under_home(tmp_path, monkeypatch):
    home_dir = tmp_path / "home"
    projects_root = tmp_path / "projects"
    home_dir.mkdir()
    projects_root.mkdir()

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("PROJECTS_ROOT", str(projects_root))
    monkeypatch.delenv("PT_EXTERNAL_BACKUP_DIR", raising=False)

    import scripts.config as config

    reloaded = importlib.reload(config)

    assert reloaded.EXTERNAL_BACKUP_DIR == home_dir / ".project-tracker" / "backups"


def test_projects_root_honours_env_override(tmp_path, monkeypatch):
    import scripts.config as config

    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path))

    assert config.projects_root() == tmp_path


@pytest.mark.parametrize("env_value", ["", None], ids=["empty_env", "unset"])
def test_projects_root_falls_back_to_home(tmp_path, monkeypatch, env_value):
    import scripts.config as config

    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))
    if env_value is None:
        monkeypatch.delenv("PROJECTS_ROOT", raising=False)
    else:
        monkeypatch.setenv("PROJECTS_ROOT", env_value)

    assert config.projects_root() == home_dir / "projects"


def test_config_runtime_defaults_are_project_tracker_local(monkeypatch):
    import scripts.config as config

    monkeypatch.delenv("PT_RESOURCES_FILE", raising=False)

    reloaded = importlib.reload(config)

    assert reloaded.EXTERNAL_RESOURCES_FILE == reloaded.PROJECT_ROOT / "EXTERNAL_RESOURCES.yaml"
    assert "project-scaffolding" not in str(reloaded.EXTERNAL_RESOURCES_FILE)


@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_resources_file_honors_pt_resources_file_override(monkeypatch, tmp_path, form):
    import scripts.config as config

    registry = tmp_path / "custom registry's $NAME.yaml"
    monkeypatch.chdir(tmp_path)
    configured = registry.name if form == "relative" else str(registry)
    monkeypatch.setenv("PT_RESOURCES_FILE", configured)

    spec = importlib.util.spec_from_file_location("synthetic_config", config.__file__)
    synthetic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(synthetic)

    assert synthetic.EXTERNAL_RESOURCES_FILE == Path(configured)
    assert synthetic.EXTERNAL_RESOURCES_FILE.resolve() == registry.resolve()


def test_graph_builder_uses_bundled_scan_config_without_warning(caplog):
    import scripts.discovery.graph_builder as graph_builder

    with caplog.at_level(logging.WARNING):
        reloaded = importlib.reload(graph_builder)

    assert reloaded.CONFIG_PATH == reloaded.Path(__file__).resolve().parents[1] / "config" / "scan_config.yaml"
    assert reloaded.CONFIG_PATH.exists()
    assert reloaded.SCAN_EXTENSIONS[".py"] == "python"
    assert reloaded.ORPHAN_EXEMPT_PROJECTS == {"root"}
    assert "project-scaffolding" not in str(reloaded.CONFIG_PATH)
    assert "Config not found" not in caplog.text


def test_is_valid_cron_accepts_special_alias_without_croniter(monkeypatch):
    monkeypatch.setattr(cron_monitor, "croniter", None)

    assert cron_monitor.is_valid_cron("@daily") is True
    assert cron_monitor.is_valid_cron("0 0 * * *") is False


def test_get_expected_next_run_handles_missing_or_broken_croniter(monkeypatch):
    last_run = datetime(2026, 3, 1, 12, 0, 0)

    monkeypatch.setattr(cron_monitor, "croniter", None)
    assert cron_monitor.get_expected_next_run("0 * * * *", last_run) is None
    assert cron_monitor.get_expected_next_run("@hourly", last_run) == last_run + timedelta(hours=1)

    def rejecting_croniter(*_args, **_kwargs):
        raise ValueError("croniter cannot step this schedule")

    monkeypatch.setattr(cron_monitor, "croniter", rejecting_croniter)
    assert cron_monitor.get_expected_next_run("0 * * * *", last_run) is None


def test_get_expected_next_run_raises_unexpected_croniter_errors(monkeypatch):
    """#6900: a croniter bug must surface, not silently disable the missed-run check."""
    def broken_croniter(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cron_monitor, "croniter", broken_croniter)
    with pytest.raises(RuntimeError, match="boom"):
        cron_monitor.get_expected_next_run("0 * * * *", datetime(2026, 3, 1, 12, 0, 0))


def test_is_valid_cron_rejects_malformed_expression_and_raises_on_bugs(monkeypatch):
    """#6900: only croniter's own rejection (a ValueError) means "invalid"."""
    assert cron_monitor.is_valid_cron("not a cron") is False

    def broken_croniter(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cron_monitor, "croniter", broken_croniter)
    with pytest.raises(RuntimeError, match="boom"):
        cron_monitor.is_valid_cron("0 0 * * *")


def test_fresh_database_check_allows_scanned_projects_without_tasks(tmp_path, monkeypatch):
    db_path = tmp_path / "tracker.db"
    fingerprint_path = tmp_path / ".db-fingerprint"

    monkeypatch.delenv("PT_ALLOW_FRESH_DB", raising=False)
    monkeypatch.delenv("PT_TEST_MODE", raising=False)
    monkeypatch.setattr(schema, "sys", types.SimpleNamespace(modules={k: v for k, v in sys.modules.items() if k != "pytest"}))

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
    cursor.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
    cursor.execute("INSERT INTO projects (id, name) VALUES ('smart-invoice-workflow', 'Smart Invoice Workflow')")
    conn.commit()
    conn.close()

    fingerprint_path.write_text("test-fingerprint")

    _check_fresh_database(Path(db_path))


def test_fresh_database_check_blocks_when_prior_task_activity_exists(tmp_path, monkeypatch):
    db_path = tmp_path / "tracker.db"
    fingerprint_path = tmp_path / ".db-fingerprint"
    backup_dir = tmp_path / "backups"

    monkeypatch.delenv("PT_ALLOW_FRESH_DB", raising=False)
    monkeypatch.delenv("PT_TEST_MODE", raising=False)
    monkeypatch.setattr(schema, "sys", types.SimpleNamespace(modules={k: v for k, v in sys.modules.items() if k != "pytest"}))

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
    cursor.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
    cursor.execute("CREATE TABLE task_history (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, project_id TEXT, event_type TEXT, timestamp TEXT)")
    cursor.execute("INSERT INTO projects (id, name) VALUES ('smart-invoice-workflow', 'Smart Invoice Workflow')")
    cursor.execute("INSERT INTO task_history (task_id, project_id, event_type, timestamp) VALUES (1, 'smart-invoice-workflow', 'created', '2026-03-26T00:00:00')")
    conn.commit()
    conn.close()

    fingerprint_path.write_text("test-fingerprint")
    backup_dir.mkdir()
    (backup_dir / "tasks_safety_backup_20260327_000000.json").write_text("{}")

    with pytest.raises(Exception) as excinfo:
        _check_fresh_database(Path(db_path))

    assert "SUSPICIOUS EMPTY TASK BOARD DETECTED" in str(excinfo.value)


def test_launch_prefers_dot_venv_python(tmp_path):
    project_root = tmp_path / "project-tracker"
    dot_venv_python = project_root / ".venv" / "bin" / "python"
    dot_venv_python.parent.mkdir(parents=True)
    dot_venv_python.write_text("")

    resolved = pt._resolve_dashboard_python(project_root)

    assert resolved == str(dot_venv_python)


def test_launch_exits_when_no_venv_exists(tmp_path):
    project_root = tmp_path / "project-tracker"
    project_root.mkdir()

    with pytest.raises(SystemExit) as excinfo:
        pt._resolve_dashboard_python(project_root)

    assert excinfo.value.code == 1
