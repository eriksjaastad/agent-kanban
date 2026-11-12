import importlib
from pathlib import Path

import pytest


def _reload_modules():
    import scripts.config as config
    import scripts.db.schema as schema
    import scripts.db.manager as manager
    import scripts.discovery.project_scanner as project_scanner
    import scripts.pt as pt

    importlib.reload(config)
    importlib.reload(schema)
    importlib.reload(manager)
    importlib.reload(project_scanner)
    importlib.reload(pt)

    return config, schema, manager, project_scanner, pt


@pytest.fixture()
def scan_env(tmp_path, monkeypatch):
    base_dir = tmp_path / "projects"
    base_dir.mkdir()
    db_path = tmp_path / "tracker.db"

    monkeypatch.setenv("PROJECTS_ROOT", str(base_dir))
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    config, schema, manager, project_scanner, pt = _reload_modules()
    project_scanner._ptignore_cache = None

    return {
        "base_dir": base_dir,
        "db_path": db_path,
        "config": config,
        "schema": schema,
        "manager": manager,
        "project_scanner": project_scanner,
        "pt": pt,
    }


def _make_project_dir(base_dir: Path, name: str, marker: str = "README.md") -> Path:
    project_dir = base_dir / name
    project_dir.mkdir()
    (project_dir / marker).write_text("# Project\n")
    return project_dir


def _project_metadata(project_dir: Path, project_id: str, status: str = "active") -> dict:
    return {
        "id": project_id,
        "name": project_dir.name,
        "path": str(project_dir),
        "status": status,
        "phase": None,
        "description": None,
        "last_modified": "2026-01-01T00:00:00",
        "completion_pct": 0,
        "ai_agents": [],
        "cron_jobs": [],
        "services": [],
        "is_infrastructure": False,
        "has_index": False,
        "index_is_valid": False,
        "index_updated_at": None,
        "project_type": "standard",
    }


def test_add_project_upsert_preserves_tasks(scan_env):
    # Prevents cascade wipe incidents caused by INSERT OR REPLACE
    schema = scan_env["schema"]
    manager = scan_env["manager"]

    schema.init_db()
    db = manager.DatabaseManager()

    db.add_project(project_id="demo", name="Demo", path="demo", status="active")
    db.add_task(text="Keep me", project_id="demo", status="Backlog", priority=None, prompt=None)

    before = db.get_tasks(project_id="demo")
    assert len(before) == 1

    db.add_project(project_id="demo", name="Demo", path="demo", status="paused")

    after = db.get_tasks(project_id="demo")
    assert len(after) == 1
    assert after[0]["text"] == "Keep me"


def test_add_project_upsert_updates_fields(scan_env):
    # Ensures upsert updates metadata without delete/insert
    schema = scan_env["schema"]
    manager = scan_env["manager"]

    schema.init_db()
    db = manager.DatabaseManager()

    db.add_project(project_id="demo", name="Demo", path="demo", status="paused")
    db.add_project(project_id="demo", name="Demo", path="demo", status="active")

    project = db.get_project("demo")
    assert project["status"] == "active"


def test_delete_project_cascades_tasks(scan_env):
    # Ensures legitimate deletes still cascade to tasks
    schema = scan_env["schema"]
    manager = scan_env["manager"]

    schema.init_db()
    db = manager.DatabaseManager()

    db.add_project(project_id="demo", name="Demo", path="demo", status="active")
    db.add_task(text="Disposable", project_id="demo", status="Backlog", priority=None, prompt=None)

    assert len(db.get_tasks(project_id="demo")) == 1
    
    # Authorize destructive op before calling delete_project
    db.authorize("delete_project", reason="delete_project is tested", project_id="demo")
    
    assert len(db.get_tasks(project_id="demo")) == 0


def test_scan_integrity_check_zero_projects_aborts(scan_env, monkeypatch, capsys):
    # Prevents empty scan from wiping data
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    schema.init_db()
    db = manager.DatabaseManager()
    db.add_project(project_id="demo", name="Demo", path="demo", status="active")

    monkeypatch.setattr(pt, "discover_projects", lambda *_args, **_kwargs: [])

    pt._scan_impl(no_graph=True, dry_run=False, force=False)
    out = capsys.readouterr().out

    assert "Scan found 0 projects" in out
    assert len(db.get_all_projects()) == 1


def test_scan_integrity_check_drop_aborts_without_force(scan_env, monkeypatch, capsys):
    # Prevents steep drop from silently overwriting metadata
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    schema.init_db()
    db = manager.DatabaseManager()
    for idx in range(4):
        db.add_project(
            project_id=f"proj-{idx}",
            name=f"Proj {idx}",
            path=f"proj-{idx}",
            status="active"
        )

    project_dir = _make_project_dir(scan_env["base_dir"], "proj-0")
    monkeypatch.setattr(
        pt,
        "discover_projects",
        lambda *_args, **_kwargs: [_project_metadata(project_dir, "proj-0")]
    )

    pt._scan_impl(no_graph=True, dry_run=False, force=False)
    out = capsys.readouterr().out

    assert "far fewer projects" in out
    assert len(db.get_all_projects()) == 4


def test_scan_integrity_check_force_allows(scan_env, monkeypatch):
    # Ensures --force override allows scan to proceed
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    schema.init_db()
    db = manager.DatabaseManager()
    for idx in range(2):
        db.add_project(
            project_id=f"proj-{idx}",
            name=f"Proj {idx}",
            path=f"proj-{idx}",
            status="paused"
        )

    project_dir = _make_project_dir(scan_env["base_dir"], "proj-0")
    monkeypatch.setattr(
        pt,
        "discover_projects",
        lambda *_args, **_kwargs: [_project_metadata(project_dir, "proj-0", status="active")]
    )
    monkeypatch.setattr(pt, "scan_health_parallel", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pt, "parse_external_resources", lambda: {})

    pt._scan_impl(no_graph=True, dry_run=False, force=True)

    proj = db.get_project("proj-0")
    assert proj["status"] == "active"


def test_scan_per_project_transaction_rolls_back_on_failure(scan_env, monkeypatch):
    # Prevents partial updates when a project sync fails mid-transaction
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    import db.backend_manager as backend_manager

    db = manager.DatabaseManager()
    db.add_project(project_id="good", name="Good", path="good", status="paused")
    db.add_project(project_id="bad", name="Bad", path="bad", status="paused")

    good_dir = _make_project_dir(scan_env["base_dir"], "good")
    bad_dir = _make_project_dir(scan_env["base_dir"], "bad")

    projects = [
        _project_metadata(good_dir, "good", status="active"),
        _project_metadata(bad_dir, "bad", status="active"),
    ]

    monkeypatch.setattr(pt, "discover_projects", lambda *_args, **_kwargs: projects)
    monkeypatch.setattr(pt, "scan_health_parallel", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pt, "parse_external_resources", lambda: {})

    def _raise_on_bad(self, cursor, project_id, cron_jobs, allow_deletes=True):
        if project_id == "bad":
            raise RuntimeError("Injected failure")
        return {"added": 0, "updated": 0, "deleted": 0}

    monkeypatch.setattr(backend_manager.DatabaseManager, "_sync_cron_jobs_with_cursor", _raise_on_bad)

    pt._scan_impl(no_graph=True, dry_run=False, force=True)

    assert db.get_project("good")["status"] == "active"
    assert db.get_project("bad")["status"] == "paused"


def test_scan_per_project_transaction_commits_success(scan_env, monkeypatch):
    # Ensures successful projects all commit together
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    schema.init_db()
    db = manager.DatabaseManager()
    db.add_project(project_id="a", name="A", path="a", status="paused")
    db.add_project(project_id="b", name="B", path="b", status="paused")

    a_dir = _make_project_dir(scan_env["base_dir"], "a")
    b_dir = _make_project_dir(scan_env["base_dir"], "b")

    projects = [
        _project_metadata(a_dir, "a", status="active"),
        _project_metadata(b_dir, "b", status="active"),
    ]

    monkeypatch.setattr(pt, "discover_projects", lambda *_args, **_kwargs: projects)
    monkeypatch.setattr(pt, "scan_health_parallel", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pt, "parse_external_resources", lambda: {})

    pt._scan_impl(no_graph=True, dry_run=False, force=True)

    assert db.get_project("a")["status"] == "active"
    assert db.get_project("b")["status"] == "active"


def test_scan_leaves_a_projects_info_entries_alone(scan_env, monkeypatch):
    """pt scan writes the project's row, agents, cron jobs and services; `pt info` is not its to touch."""
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    schema.init_db()
    db = manager.DatabaseManager()

    project_dir = _make_project_dir(scan_env["base_dir"], "invoices")
    project = _project_metadata(project_dir, "invoices", status="active")
    db.add_project(project_id="invoices", name="invoices", path=str(project_dir), status="active")
    db.set_info("portfolio_label", "[AP]", project_id="invoices")

    monkeypatch.setattr(pt, "discover_projects", lambda *_args, **_kwargs: [project])
    monkeypatch.setattr(pt, "scan_health_parallel", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pt, "parse_external_resources", lambda: {})

    pt._scan_impl(no_graph=True, dry_run=False, force=True)

    assert db.get_info(project_id="invoices", key="portfolio_label")[0]["value"] == "[AP]"


def test_scan_dry_run_skips_writes(scan_env, monkeypatch, capsys):
    # Prevents dry-run from mutating the database
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    schema.init_db()
    db = manager.DatabaseManager()
    db.add_project(project_id="demo", name="Demo", path="demo", status="paused")

    project_dir = _make_project_dir(scan_env["base_dir"], "demo")
    monkeypatch.setattr(
        pt,
        "discover_projects",
        lambda *_args, **_kwargs: [_project_metadata(project_dir, "demo", status="active")]
    )

    pt._scan_impl(no_graph=True, dry_run=True, force=True)
    out = capsys.readouterr().out

    assert "Dry-run" in out
    assert db.get_project("demo")["status"] == "paused"


def test_scan_dry_run_reports_projects(scan_env, monkeypatch, capsys):
    # Ensures dry-run still reports discovery count
    schema = scan_env["schema"]
    manager = scan_env["manager"]
    pt = scan_env["pt"]

    schema.init_db()
    manager.DatabaseManager()

    project_dir = _make_project_dir(scan_env["base_dir"], "demo")
    monkeypatch.setattr(
        pt,
        "discover_projects",
        lambda *_args, **_kwargs: [_project_metadata(project_dir, "demo")]
    )

    pt._scan_impl(no_graph=True, dry_run=True, force=True)
    out = capsys.readouterr().out

    assert "Found 1 projects" in out


def test_ptignore_skips_directories(scan_env):
    # Prevents scanning of explicitly ignored directories
    project_scanner = scan_env["project_scanner"]
    base_dir = scan_env["base_dir"]

    (base_dir / ".ptignore").write_text("# Ignore list\nskip-me\n\n")

    _make_project_dir(base_dir, "keep-me")
    _make_project_dir(base_dir, "skip-me")

    project_scanner._ptignore_cache = None
    projects = project_scanner.discover_projects(base_dir)

    names = {p["name"] for p in projects}
    assert "keep-me" in names
    assert "skip-me" not in names


def test_ptignore_missing_file_is_safe(scan_env):
    # Missing .ptignore should not block discovery
    project_scanner = scan_env["project_scanner"]
    base_dir = scan_env["base_dir"]

    _make_project_dir(base_dir, "keep-me")
    project_scanner._ptignore_cache = None

    projects = project_scanner.discover_projects(base_dir)
    names = {p["name"] for p in projects}

    assert "keep-me" in names


@pytest.mark.parametrize("registry", [None, "other: {}\n", "projects: [unclosed\n"])
def test_scan_aborts_before_writes_when_resources_registry_is_unusable(scan_env, monkeypatch, tmp_path, registry):
    """#6900: a missing, keyless or unparseable EXTERNAL_RESOURCES.yaml used to
    read as "no services", and the services sync (deletes allowed) erased every
    project's service rows. The scan must abort first and keep them."""
    manager = scan_env["manager"]
    pt = scan_env["pt"]
    from scripts.discovery.external_resources_parser import parse_external_resources

    db = manager.DatabaseManager()
    db.add_project(project_id="proj-0", name="proj-0", path="proj-0", status="paused")
    db.add_service(project_id="proj-0", service_name="Doppler", purpose="secrets", cost_monthly=0)

    registry_path = tmp_path / "EXTERNAL_RESOURCES.yaml"
    if registry is not None:
        registry_path.write_text(registry)

    project_dir = _make_project_dir(scan_env["base_dir"], "proj-0")
    monkeypatch.setattr(
        pt, "discover_projects",
        lambda *_args, **_kwargs: [_project_metadata(project_dir, "proj-0", status="active")],
    )
    monkeypatch.setattr(pt, "scan_health_parallel", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pt, "parse_external_resources", lambda: parse_external_resources(registry_path))

    with pytest.raises(SystemExit) as excinfo:
        pt._scan_impl(no_graph=True, dry_run=False, force=True)
    assert excinfo.value.code == 1

    assert [s["service_name"] for s in db.get_services(project_id="proj-0")] == ["Doppler"]
    assert db.get_project("proj-0")["status"] == "paused"


@pytest.mark.parametrize("graph_section, exits", [
    ('[graph]\nignore_projects = "a"\n', True),        # broken: the rebuild fails, and so does the scan
    ('[graph]\nignore_projects = ["a"]\n', False),
    ("", False),                                        # no [graph] at all
])
def test_a_scan_whose_graph_rebuild_fails_exits_1_and_keeps_the_old_graph(scan_env, monkeypatch, tmp_path, capsys,
                                                                          graph_section, exits):
    schema, pt = scan_env["schema"], scan_env["pt"]
    schema.init_db()
    (tmp_path / "config.toml").write_text(graph_section)
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))
    graph = tmp_path / "graph.json"
    graph.write_text("old")
    monkeypatch.setattr(pt, "GRAPH_PATH", graph)
    monkeypatch.setattr(pt, "GRAPH_ANALYSIS_PATH", tmp_path / "analysis.md")
    monkeypatch.setattr(pt, "PROJECTS_BASE_DIR", str(scan_env["base_dir"]))
    monkeypatch.setattr(pt, "discover_projects", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pt, "scan_health_parallel", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(pt, "parse_external_resources", lambda: {})

    if exits:
        with pytest.raises(SystemExit) as stopped:
            pt._scan_impl(no_graph=False, dry_run=False, force=True)
        assert stopped.value.code == 1 and "[graph]" in capsys.readouterr().out and graph.read_text() == "old"
        pt._scan_impl(no_graph=True, dry_run=False, force=True)  # --no-graph never touches it
    else:
        pt._scan_impl(no_graph=False, dry_run=False, force=True)
        assert graph.read_text() != "old"


def test_scan_keeps_cron_jobs_added_with_add_cron(scan_env, monkeypatch):
    # #8983: the scanner never finds cron jobs, so a scan that pruned every cron
    # it was not told about wiped each `pt calendar add-cron` row (2026-04-06).
    schema, manager, pt = scan_env["schema"], scan_env["manager"], scan_env["pt"]
    schema.init_db()
    db = manager.DatabaseManager()
    db.add_project(project_id="app", name="app", path="app", status="active")
    from scripts.db.calendar_manager import CalendarManager
    CalendarManager().add_cron_job("app", "0 */6 * * *", "backup.sh", "6-hourly backup")

    app_dir = _make_project_dir(scan_env["base_dir"], "app")
    found = _project_metadata(app_dir, "app")
    found["cron_jobs"] = [{"schedule": "0 7 * * *", "command": "digest.sh", "description": "found by a scan"}]
    monkeypatch.setattr(pt, "discover_projects", lambda *_a, **_k: [found])
    monkeypatch.setattr(pt, "scan_health_parallel", lambda *_a, **_k: {})
    monkeypatch.setattr(pt, "parse_external_resources", lambda: {})

    pt._scan_impl(no_graph=True, dry_run=False, force=True)

    assert sorted(j["command"] for j in db.get_cron_jobs("app")) == ["backup.sh", "digest.sh"]
