"""Reminders reaches pt, the dashboard and the menu through its manifest alone."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

from addons.reminders.db import RemindersDB
from dashboard.app import app
from dashboard.common import build_navigation, iter_navigation_leaves
from pt import cli

client = TestClient(app)
ENV = {"PT_NO_BANNER": "1", "PT_SUPPRESS_MIGRATION_WARNING": "1"}


def test_pt_lists_reminders_and_runs_its_commands() -> None:
    listed = CliRunner().invoke(cli, ["addons", "list"], env=ENV)
    assert listed.exit_code == 0, listed.output
    assert "reminders  on  (public)" in listed.output
    assert CliRunner().invoke(cli, ["reminders", "--help"], env=ENV).exit_code == 0


def test_reminders_is_a_top_level_tab() -> None:
    assert client.get("/api/reminders").status_code == 200
    # 503 when the frontend is not built, as in a fresh worktree; 404 if unregistered.
    assert client.get("/reminders").status_code in (200, 503)
    item = next(i for i in client.get("/api/navigation").json()["items"] if i["id"] == "reminders")
    assert (item["label"], item["href"], item["navigation_type"]) == (
        "Reminders", "/reminders", "spa")
    leaf = next(i for i in iter_navigation_leaves(build_navigation("/reminders")) if i["id"] == "reminders")
    assert leaf["active"]


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
print(client.get("/api/reminders").status_code, client.get("/reminders").status_code,
      any(item["id"] == "reminders" for item in items))
"""


def _fresh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **ENV}, timeout=120)


def test_disable_hides_reminders_and_keeps_its_rows_until_enabled_again(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))
    RemindersDB().add_reminder("Kept while off")
    assert "Turned reminders off" in CliRunner().invoke(cli, ["addons", "disable", "reminders"], env=ENV).output

    off = _fresh("scripts/pt.py", "reminders")
    assert off.returncode != 0 and "No such command 'reminders'" in off.stderr
    assert "reminders  off" in _fresh("scripts/pt.py", "addons", "list").stdout
    api, page, in_menu = _fresh("-c", DASHBOARD_PROBE).stdout.split()
    assert (api, page, in_menu) == ("404", "404", "False")
    assert [r["name"] for r in RemindersDB().list_reminders()["active"]] == ["Kept while off"]

    assert "Turned reminders on" in CliRunner().invoke(cli, ["addons", "enable", "reminders"], env=ENV).output
    on = _fresh("scripts/pt.py", "reminders")
    assert on.returncode == 0 and "Kept while off" in on.stdout
    api, page, in_menu = _fresh("-c", DASHBOARD_PROBE).stdout.split()
    assert api == "200" and page in ("200", "503") and in_menu == "True"
