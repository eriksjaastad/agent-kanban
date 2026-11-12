"""Codebase size reaches pt, the dashboard and the Agents menu through its manifest alone."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

from addons.codebase_size.db import CodebaseSizeDB
from addons.codebase_size.scanner import RepoSize
from dashboard.app import app
from dashboard.common import build_navigation, iter_navigation_leaves
from pt import cli

client = TestClient(app)
ENV = {"PT_NO_BANNER": "1", "PT_SUPPRESS_MIGRATION_WARNING": "1"}


def test_pt_lists_codebase_size_and_runs_pt_size() -> None:
    listed = CliRunner().invoke(cli, ["addons", "list"], env=ENV)
    assert "codebase_size  on  (public)" in listed.output
    assert CliRunner().invoke(cli, ["size", "--help"], env=ENV).exit_code == 0


def test_codebase_size_sits_last_in_the_agents_group() -> None:
    assert client.get("/api/codebase-size").status_code == 200
    # 503 when the frontend is not built, as in a fresh worktree; 404 if unregistered.
    assert client.get("/codebase").status_code in (200, 503)
    agents = next(i for i in client.get("/api/navigation").json()["items"] if i["id"] == "group-agents")
    leaf = agents["children"][-1]
    assert (leaf["id"], leaf["label"], leaf["href"], leaf["navigation_type"]) == (
        "codebase", "Codebase size", "/codebase", "spa")
    assert "group" not in leaf
    assert "/codebase" in agents["match_prefixes"]
    active = next(i for i in iter_navigation_leaves(build_navigation("/codebase")) if i["id"] == "codebase")
    assert active["active"]


# Commands, routes and the menu are read once at startup, so each check below
# starts a fresh process, as a restart of pt or the dashboard would.
ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_PROBE = """
import sys
sys.path[:0] = [".", "scripts"]
from fastapi.testclient import TestClient
from dashboard.app import app
client = TestClient(app)
items = client.get("/api/navigation").json()["items"]
agents = next(i for i in items if i["id"] == "group-agents")
print(client.get("/api/codebase-size").status_code, client.get("/codebase").status_code,
      any(child["id"] == "codebase" for child in agents["children"]))
"""


def _fresh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **ENV}, timeout=120)


def test_disable_hides_codebase_size_and_keeps_its_runs_until_enabled_again(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))
    CodebaseSizeDB().save_codebase_scan([RepoSize("kept", code=7)], "2026-11-11")
    assert "Turned codebase_size off" in CliRunner().invoke(
        cli, ["addons", "disable", "codebase_size"], env=ENV).output

    off = _fresh("scripts/pt.py", "size", "--help")
    assert off.returncode != 0 and "No such command 'size'" in off.stderr
    api, page, in_menu = _fresh("-c", DASHBOARD_PROBE).stdout.split()
    assert (api, page, in_menu) == ("404", "404", "False")
    assert CodebaseSizeDB().codebase_baseline()[1]["kept"].code == 7

    assert "Turned codebase_size on" in CliRunner().invoke(
        cli, ["addons", "enable", "codebase_size"], env=ENV).output
    assert _fresh("scripts/pt.py", "size", "--help").returncode == 0
    api, page, in_menu = _fresh("-c", DASHBOARD_PROBE).stdout.split()
    assert api == "200" and page in ("200", "503") and in_menu == "True"
