import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure the project root is on sys.path so that `scripts` and `dashboard`
# are importable in any environment (including sandboxed uv run on the Mini).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

# Phase 2.1a: the pt root callback emits a stderr warning when the DB
# has unapplied migrations. Humans see it; CliRunner merges it into
# ``result.output`` and breaks JSON-parsing tests that invoke real pt
# subcommands. Suppress by default for tests; tests that specifically
# want to exercise the warning delete this env var per-test.
os.environ.setdefault("PT_SUPPRESS_MIGRATION_WARNING", "1")

# Keep the destructive-operation audit log out of the developer's real one.
# `_audit_destructive` records every in-process deletion so a future incident
# can be attributed; tests that exercise those code paths were writing
# synthetic pytest-tmp entries straight into it (51 of 51 entries were test
# chaff), burying real evidence in the file that exists to preserve it.
# Session-scoped so a single run's records stay together if anyone inspects them.
_TEST_DESTRUCTIVE_LOG = (
    Path(tempfile.gettempdir()) / f"pt-destructive-tests-{os.getpid()}.log"
)
os.environ.setdefault("PT_DESTRUCTIVE_LOG_PATH", str(_TEST_DESTRUCTIVE_LOG))

# Keep migration backups out of the REAL external backup directory.
#
# `scripts/config.py` resolves EXTERNAL_BACKUP_DIR at import to
# ~/.project-tracker/backups — the second location DECISIONS.md requires so
# that losing the project directory does not lose the data. Migrations write a
# pre-flight backup to it, and the test suite runs migrations, so every run
# has been dropping 135KB snapshots into the disaster-recovery directory since
# April. 9,641 files were found there on 2026-09-17, effectively burying the
# real backups in test chaff.
#
# Set before anything imports scripts.config, because it is read once at
# import and cached in a module constant.
#
# FORCE the test override — use direct assignment instead of setdefault so
# that if PT_EXTERNAL_BACKUP_DIR is inherited from the outer environment,
# the test-scoped temp directory wins. Prevents leaks when pytest is run
# with the variable already set.
_TEST_EXTERNAL_BACKUPS = Path(tempfile.gettempdir()) / f"pt-test-backups-{os.getpid()}"
os.environ["PT_EXTERNAL_BACKUP_DIR"] = str(_TEST_EXTERNAL_BACKUPS)

# The schema layer refuses to initialise a database that is unexpectedly
# empty — a guard against the 2026-01-27 incident, where an empty database
# meant the real one had been destroyed. Every test database starts empty on
# purpose, so the guard is off for the suite and only for the suite.
os.environ.setdefault("PT_ALLOW_FRESH_DB", "1")

# Prevent collection-time imports from reaching the real database.
_TEST_DB_ROOT = Path(tempfile.mkdtemp(prefix="pt-test-db-"))
os.environ["PT_DB_PATH"] = str(_TEST_DB_ROOT / "collection.db")

@pytest.fixture(scope="session")
def _template(tmp_path_factory) -> Path:
    """A database built once through create_database and copied per test.

    It is the database production creates: schema.sql already contains every
    migration (a test fails until `make schema` folds a new one in), so the
    tests never run against a schema nobody ships.
    """
    from db.schema import create_database

    template = tmp_path_factory.mktemp("local-template") / "tracker.db"
    create_database(template)
    return template


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch, _template):
    target = tmp_path / "_local" / "tracker.db"
    target.parent.mkdir()
    shutil.copy(_template, target)
    monkeypatch.setenv("PT_DB_PATH", str(target))
    # No test reads this machine's private integration config; a test that
    # needs one writes its own and points PT_CONFIG at it.
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "_local" / "config.toml"))
    # Nor this machine's Agent Chat human (agent-chat/identity.py): set but empty.
    monkeypatch.setenv("AGENT_CHAT_HUMAN", "")
    monkeypatch.setenv("PT_EXTERNAL_BACKUP_DIR", str(tmp_path / "external-backups"))
    from db import attachment_paths
    monkeypatch.setattr(attachment_paths, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    from tests.local_db_support import LocalTestSupport
    from db.operations import ProjectTrackerOps
    for name in ("seed", "seed_task", "count_rows", "_SEEDABLE_TASK_COLUMNS", "_COUNTABLE_TABLES"):
        monkeypatch.setattr(ProjectTrackerOps, name, getattr(LocalTestSupport, name), raising=False)
    return target

@pytest.fixture
def memory_store(tmp_path, monkeypatch):
    """Turn on the memory integration with its store at tmp_path/"memory-store".

    Writes the test's own local config (scripts/config.py [memory]) and returns
    a writer; call it with extra [memory] keys to rewrite the config.
    """
    root = tmp_path / "memory-store"
    root.mkdir(exist_ok=True)

    def configure(**extra):
        lines = [f'root = "{root}"'] + [f'{key} = "{value}"' for key, value in extra.items()]
        path = tmp_path / "_local" / "config.toml"
        path.write_text("[memory]\n" + "\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setenv("PT_CONFIG", str(path))
        return root

    configure()
    configure.root = root
    return configure


@pytest.fixture
def db(isolated_database):
    from db.manager import DatabaseManager
    return DatabaseManager()

@pytest.fixture
def local_backend(isolated_database):
    from db.backend_manager import DatabaseManager
    return DatabaseManager(isolated_database)
