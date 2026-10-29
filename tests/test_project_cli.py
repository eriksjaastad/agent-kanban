"""Tests for manual project CLI commands."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.schema import create_database


@pytest.fixture
def isolated_tracker(monkeypatch):
    """Use a disposable projects root and tracker DB for CLI tests."""
    with tempfile.TemporaryDirectory() as tmp:
        projects_root = Path(tmp)
        db_path = projects_root / "tracker.db"
        create_database(db_path)

        monkeypatch.setenv("PT_DB_PATH", str(db_path))
        monkeypatch.setenv("PROJECTS_ROOT", str(projects_root))

        for mod in ("scripts.pt", "scripts.config", "db.manager"):
            sys.modules.pop(mod, None)

        from db.manager import DatabaseManager as DM
        from scripts.pt import cli as pt_cli

        yield {
            "projects_root": projects_root,
            "db": DM(),
            "cli": pt_cli,
        }


def test_project_add_virtual_creates_board_only_project(isolated_tracker):
    runner = CliRunner()

    result = runner.invoke(
        isolated_tracker["cli"],
        ["project", "add", "openclaw", "--name", "OpenClaw", "--virtual"],
    )

    assert result.exit_code == 0, result.output
    assert "Added virtual project" in result.output
    assert not (isolated_tracker["projects_root"] / "openclaw").exists()

    project = isolated_tracker["db"].get_project("openclaw")
    assert project is not None
    assert project["name"] == "OpenClaw"
    assert project["path"] == "virtual://openclaw"
    assert project["project_type"] == "virtual"

    task = isolated_tracker["db"].add_task(
        text="Track OpenClaw board-only work",
        project_id="openclaw",
    )
    assert task["project_id"] == "openclaw"


def test_project_add_requires_virtual_or_existing_path(isolated_tracker):
    runner = CliRunner()

    bad_slug = runner.invoke(isolated_tracker["cli"], ["project", "add", "open claw", "--virtual"])
    assert bad_slug.exit_code != 0
    assert "simple slug" in bad_slug.output

    missing_mode = runner.invoke(isolated_tracker["cli"], ["project", "add", "openclaw"])
    assert missing_mode.exit_code != 0
    assert "Provide --virtual" in missing_mode.output

    missing_path = runner.invoke(
        isolated_tracker["cli"],
        ["project", "add", "openclaw", "--path", str(isolated_tracker["projects_root"] / "openclaw")],
    )
    assert missing_path.exit_code != 0
    assert "does not exist" in missing_path.output


def test_project_add_refuses_existing_project(isolated_tracker):
    runner = CliRunner()
    first = runner.invoke(isolated_tracker["cli"], ["project", "add", "openclaw", "--virtual"])
    assert first.exit_code == 0, first.output

    duplicate = runner.invoke(isolated_tracker["cli"], ["project", "add", "openclaw", "--virtual"])
    assert duplicate.exit_code != 0
    assert "already exists" in duplicate.output
