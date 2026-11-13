"""Parts that exist only where their module's file does (#8975): absence leaves the rest working.

Absence is simulated by a `present` check that says no; nothing here deletes a file.
"""

import copy

import click
import pytest
from click.testing import CliRunner
from fastapi import FastAPI

import dashboard.app as dashboard_app
import dashboard.common as common
import pt
from scripts.config import module_present

CLI_MODULES = {"commands.memory": "memory", "commands.graph": "graph", "commands.skills": "skills"}
ROUTE_MODULES = ("dashboard.routes.memory", "dashboard.routes.project_graph", "dashboard.routes.usage_stats")


def absent(*names):
    return lambda module: module not in names


def paths(application):
    return set(application.openapi()["paths"])


def test_commands_register_only_for_modules_that_exist():
    full, bare = click.Group("pt"), click.Group("pt")
    pt.register_optional_commands(full)
    pt.register_optional_commands(bare, present=absent(*CLI_MODULES))
    assert set(full.commands) == {name for module, name in CLI_MODULES.items() if module_present(module)}
    assert not set(bare.commands)


def test_a_present_module_that_fails_to_import_still_raises(monkeypatch):
    monkeypatch.setattr(pt, "OPTIONAL_COMMAND_GROUPS", (("commands.no_such_module_8975", "x"),))
    with pytest.raises(ImportError):
        pt.register_optional_commands(click.Group("pt"), present=lambda module: True)


def test_doctor_runs_without_the_memory_module(monkeypatch):
    monkeypatch.setattr(pt, "module_present", absent("commands.memory"))
    result = CliRunner().invoke(pt.cli, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)
    names = {check["name"] for check in payload["checks"]}
    assert {"tracker_db", "local_config"} <= names and not {"memory_readonly", "non_interactive_json"} & names
    assert "memory" not in payload["cron_setup"]["example"]


def test_a_scan_skips_the_graph_when_it_is_not_there(monkeypatch):
    def rebuilt():
        raise AssertionError("the graph builder is absent; nothing may rebuild")

    monkeypatch.setattr(pt, "module_present", absent("discovery.graph_builder"))
    monkeypatch.setattr(pt, "rebuild_project_graph", rebuilt)
    pt._rebuild_graph_or_exit()  # returns quietly
    assert ("--no-graph" in CliRunner().invoke(pt.cli, ["scan", "--help"]).output) == module_present(
        "discovery.graph_builder")


def test_the_dashboard_has_no_memory_or_graph_routes_without_their_modules():
    full, bare = FastAPI(), FastAPI()
    dashboard_app.register_optional_routers(full)
    dashboard_app.register_optional_routers(bare, present=absent(*ROUTE_MODULES))
    assert bool(paths(full)) == any(module_present(module) for module in ROUTE_MODULES)
    assert paths(bare) == set()


def test_the_rebuild_status_route_is_core():
    assert "/api/rebuild-status/{job_id}" in paths(dashboard_app.app)


def test_refresh_without_the_graph_starts_no_rebuild(monkeypatch):
    from fastapi.testclient import TestClient

    def rebuilt():
        raise AssertionError("no graph, no rebuild")

    monkeypatch.setattr(dashboard_app, "module_present", absent("discovery.graph_builder"))
    monkeypatch.setattr(dashboard_app, "discover_projects", lambda: [])
    monkeypatch.setattr(dashboard_app, "rebuild_project_graph", rebuilt)
    body = TestClient(dashboard_app.app).post("/api/refresh").json()
    assert body["status"] == "success" and "job_id" not in body and "raph" not in body["message"]


def test_navigation_has_no_memory_group_without_its_modules():
    items = common.collapse_navigation(common.drop_absent_entries(
        copy.deepcopy(common.ALL_NAVIGATION_ITEMS), present=absent(*ROUTE_MODULES)))
    assert not any(item["id"] in ("group-memory", "memory", "graph") for item in items)
    full = common.collapse_navigation(common.drop_absent_entries(
        copy.deepcopy(common.ALL_NAVIGATION_ITEMS), present=lambda module: True))
    assert any(item["id"] == "group-memory" for item in full)  # when every module is there


HIDE_GRAPH = """
import importlib.util, sys
real = importlib.util.find_spec
importlib.util.find_spec = lambda name, *a, **k: None if name == "discovery.graph_builder" else real(name, *a, **k)
sys.path.insert(0, "scripts")
import pt
pt.cli(sys.argv[1:], standalone_mode=False)
"""


def run_without_graph(*args):
    import subprocess
    import sys
    from pathlib import Path

    return subprocess.run([sys.executable, "-c", HIDE_GRAPH, *args], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[1], timeout=120)


@pytest.mark.parametrize("command", ["scan", "sync-project"])
def test_the_graph_flag_is_hidden_but_accepted_without_a_graph_builder(command):
    shown = run_without_graph(command, "--help")
    accepted = run_without_graph(command, "--no-graph", "--help")
    assert shown.returncode == accepted.returncode == 0, shown.stderr + accepted.stderr
    assert "graph" not in shown.stdout.lower() and "graph" not in accepted.stdout.lower()


@pytest.mark.parametrize("installed", [True, False])
def test_the_agentic_summary_says_whether_the_usage_charts_exist(monkeypatch, installed):
    from fastapi.testclient import TestClient

    from dashboard.routes import agentic

    monkeypatch.setattr(agentic, "module_present", lambda module: installed and module == "dashboard.routes.usage_stats")
    body = TestClient(dashboard_app.app).get("/api/agentic/summary").json()
    assert body["usage_stats"] is installed


def test_config_show_reports_the_memory_store_only_where_its_command_exists(monkeypatch):
    from commands import config as config_commands
    import json

    monkeypatch.setenv("PT_MEMORY_DB_PATH", "/tmp/elsewhere.db")
    monkeypatch.setattr(config_commands, "module_present", absent("commands.memory"))
    monkeypatch.setattr(pt, "module_present", absent("commands.memory"))
    shown = CliRunner().invoke(pt.cli, ["config", "show", "--effective", "--json"])
    payload = json.loads(shown.output)
    assert "memory_db_path" not in payload and "PT_MEMORY_DB_PATH" not in payload["env"]
    assert "memory" not in shown.output.lower()
    assert CliRunner().invoke(pt.cli, ["doctor", "--json"]).exit_code == 0
    monkeypatch.setattr(config_commands, "module_present", lambda module: True)
    payload = config_commands._effective_config_payload()
    assert payload["memory_db_path"] == "/tmp/elsewhere.db" and payload["env"]["PT_MEMORY_DB_PATH"] == "/tmp/elsewhere.db"
