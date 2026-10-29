"""Tests for the pt info command and project_info key-value store.

Covers:
- DatabaseManager CRUD for project_info (get, set, delete)
- Global vs project-scoped entries
- Upsert behavior (overwrite existing keys)
- CLI commands: info (default), set, get, delete, list
- JSON output mode
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.manager import DatabaseManager
from db.schema import create_database
from pt import info_group


def _setup_db(tmp_path: Path) -> tuple[Path, DatabaseManager]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    return db_path, db


def _invoke_cli(db_path: Path, args: list) -> object:
    """Invoke info_group CLI with a patched DatabaseManager pointing to test DB."""
    runner = CliRunner()
    with patch("pt.DatabaseManager", lambda: DatabaseManager(db_path)):
        return runner.invoke(info_group, args, catch_exceptions=False)


# --- DatabaseManager tests ---


class TestProjectInfoDB:
    def test_get_info_empty(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        entries = db.get_info()
        assert entries == []

    def test_set_and_get_global(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("domain", "example.com")
        entries = db.get_info()
        assert len(entries) == 1
        assert entries[0]["key"] == "domain"
        assert entries[0]["value"] == "example.com"
        assert entries[0]["project_id"] is None

    def test_set_and_get_project_scoped(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("tech_stack", "Python", project_id="my-project")
        # Global should be empty
        assert db.get_info() == []
        # Project-scoped should have the entry
        entries = db.get_info(project_id="my-project")
        assert len(entries) == 1
        assert entries[0]["key"] == "tech_stack"
        assert entries[0]["value"] == "Python"

    def test_upsert_overwrites(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("domain", "old.com")
        db.set_info("domain", "new.com")
        entries = db.get_info()
        assert len(entries) == 1
        assert entries[0]["value"] == "new.com"

    def test_get_by_key(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("a", "1")
        db.set_info("b", "2")
        entries = db.get_info(key="a")
        assert len(entries) == 1
        assert entries[0]["value"] == "1"

    def test_get_all_entries(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("global_key", "gval")
        db.set_info("proj_key", "pval", project_id="proj")
        entries = db.get_info(project_id="__all__")
        assert len(entries) == 2

    def test_delete_global(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("domain", "example.com")
        assert db.delete_info("domain") is True
        assert db.get_info() == []

    def test_delete_project_scoped(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("key", "val", project_id="proj")
        assert db.delete_info("key", project_id="proj") is True
        assert db.get_info(project_id="proj") == []

    def test_delete_nonexistent(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        assert db.delete_info("nope") is False

    def test_scopes_are_independent(self, tmp_path: Path):
        _, db = _setup_db(tmp_path)
        db.set_info("key", "global_val")
        db.set_info("key", "proj_val", project_id="proj")
        global_entries = db.get_info(key="key")
        proj_entries = db.get_info(project_id="proj", key="key")
        assert global_entries[0]["value"] == "global_val"
        assert proj_entries[0]["value"] == "proj_val"


# --- CLI tests ---


class TestProjectInfoCLI:
    def test_info_empty(self, tmp_path: Path):
        db_path, _ = _setup_db(tmp_path)
        result = _invoke_cli(db_path, [])
        assert "empty" in result.output

    def test_set_and_show(self, tmp_path: Path):
        db_path, _ = _setup_db(tmp_path)
        result = _invoke_cli(db_path, ["set", "domain", "example.com"])
        assert "Set domain = example.com" in result.output

    def test_get_existing(self, tmp_path: Path):
        db_path, db = _setup_db(tmp_path)
        db.set_info("domain", "example.com")
        result = _invoke_cli(db_path, ["get", "domain"])
        assert "example.com" in result.output

    def test_get_missing(self, tmp_path: Path):
        db_path, _ = _setup_db(tmp_path)
        result = _invoke_cli(db_path, ["get", "nope"])
        assert "No entry found" in result.output

    def test_delete_existing(self, tmp_path: Path):
        db_path, db = _setup_db(tmp_path)
        db.set_info("domain", "example.com")
        result = _invoke_cli(db_path, ["delete", "domain"])
        assert "Deleted" in result.output

    def test_delete_missing(self, tmp_path: Path):
        db_path, _ = _setup_db(tmp_path)
        result = _invoke_cli(db_path, ["delete", "nope"])
        assert "No entry found" in result.output

    def test_list_all(self, tmp_path: Path):
        db_path, db = _setup_db(tmp_path)
        db.set_info("gkey", "gval")
        db.set_info("pkey", "pval", project_id="proj")
        result = _invoke_cli(db_path, ["list"])
        assert "Global:" in result.output
        assert "proj:" in result.output

    def test_json_output(self, tmp_path: Path):
        db_path, db = _setup_db(tmp_path)
        db.set_info("domain", "example.com")
        result = _invoke_cli(db_path, ["--json"])
        assert '"key": "domain"' in result.output
