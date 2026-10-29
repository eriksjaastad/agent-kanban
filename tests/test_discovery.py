import pytest
import tempfile
from pathlib import Path
from scripts.discovery.project_scanner import discover_projects, extract_project_metadata


@pytest.fixture
def temp_project():
    """Create a temporary project structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Create a mock project
        project_path = tmp_path / "test-project"
        project_path.mkdir()

        # Add README.md
        (project_path / "README.md").write_text("# Test Project\nThis is a test.")

        # Add CLAUDE.md as project marker
        (project_path / "CLAUDE.md").write_text("# Test Project Config\n")

        yield project_path


def test_extract_project_metadata(temp_project):
    """Test the project metadata extraction."""
    project_data = extract_project_metadata(temp_project)

    assert project_data["name"] == "test-project"
    assert project_data["description"] == "This is a test."


def test_only_top_level_directories_are_projects(tmp_path: Path):
    """A project inside another directory is not discovered: the scan is one level deep."""
    (tmp_path / "group" / "nested").mkdir(parents=True)
    (tmp_path / "group" / "nested" / "README.md").write_text("# nested\n")

    assert discover_projects(tmp_path) == []


def test_external_resources_parse_error_raises(tmp_path):
    """#6900: `pt scan` syncs services with deletes allowed, so a broken
    EXTERNAL_RESOURCES.yaml must raise instead of reading as "no services"."""
    import yaml

    from scripts.discovery.external_resources_parser import parse_external_resources

    broken = tmp_path / "EXTERNAL_RESOURCES.yaml"
    broken.write_text("projects:\n  demo: [unclosed\n")
    with pytest.raises(yaml.YAMLError):
        parse_external_resources(broken)


def test_discover_projects_raises_when_base_cannot_be_listed(tmp_path, monkeypatch):
    """#6900: an unlistable projects base must raise, not read as "no projects"
    (the dashboard refresh reported that as a successful 0-project refresh)."""
    real_iterdir = Path.iterdir

    def guarded_iterdir(self):
        if self == tmp_path:
            raise PermissionError("denied")
        return real_iterdir(self)

    (tmp_path / "proj").mkdir()
    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    with pytest.raises(PermissionError):
        discover_projects(tmp_path)


def test_external_resources_missing_file_raises(tmp_path):
    """#6900: the registry ships in the repo, so a missing file is a
    misconfiguration, never "no services" (which `pt scan` would delete)."""
    from scripts.discovery.external_resources_parser import parse_external_resources

    with pytest.raises(FileNotFoundError, match="PT_RESOURCES_FILE"):
        parse_external_resources(tmp_path / "EXTERNAL_RESOURCES.yaml")


@pytest.mark.parametrize("body", ["", "other: {}\n", "projects:\n", "projects: [a, b]\n"])
def test_external_resources_without_projects_mapping_raises(tmp_path, body):
    from scripts.discovery.external_resources_parser import parse_external_resources

    path = tmp_path / "EXTERNAL_RESOURCES.yaml"
    path.write_text(body)
    with pytest.raises(ValueError, match="no 'projects' mapping"):
        parse_external_resources(path)


def test_external_resources_valid_registry_still_parses(tmp_path):
    from scripts.discovery.external_resources_parser import parse_external_resources

    path = tmp_path / "EXTERNAL_RESOURCES.yaml"
    path.write_text(
        "projects:\n"
        "  demo:\n"
        "    services:\n"
        "      - {name: Doppler, cost: 0, purpose: secrets}\n"
        "  empty: {}\n"
    )
    assert parse_external_resources(path) == {
        "demo": [{"service_name": "Doppler", "cost_monthly": 0, "purpose": "secrets"}]
    }


def test_get_all_alerts_runs_live_detectors(tmp_path):
    """The aggregate still runs the surviving detectors (stalled, cron) and sorts them."""
    from scripts.discovery.alert_detector import get_all_alerts

    projects = [
        {"id": "old", "name": "Old", "path": str(tmp_path), "last_modified": "2020-01-01T00:00:00Z",
         "cron_jobs": []},
        {"id": "new", "name": "New", "path": str(tmp_path), "last_modified": "2999-01-01T00:00:00Z",
         "cron_jobs": [{"schedule": "not a cron", "command": "true", "description": "bad"}]},
    ]
    alerts = get_all_alerts(projects)
    kinds = {(a["project_id"], a["type"]) for a in alerts}
    assert ("old", "stalled") in kinds
    assert any(pid == "new" and t.startswith("cron_") for pid, t in kinds)
    order = {"critical": 0, "warning": 1, "info": 2}
    assert [order[a["severity"]] for a in alerts] == sorted(order[a["severity"]] for a in alerts)
