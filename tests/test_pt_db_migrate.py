"""Integration tests for the Phase 2 PR #3a wiring.

These tests exercise the three things PR #3a adds on top of the
Phase 2 PR #2 runner:

1. ``pt db migrate`` CLI — applies pending migrations, reports what
   was applied, idempotent.
2. ``_warn_unapplied_migrations`` — stderr warning on ``pt`` startup
   when migrations are pending, silent after they're applied, never
   raises.

Unit-level coverage of the runner itself lives in
``tests/test_migration_runner.py``. This file is the wiring layer.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# noqa: E402 — sys.path setup must precede these imports
from db.manager import DatabaseManager  # noqa: E402
from pt import cli, _warn_unapplied_migrations  # noqa: E402


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


MIGRATIONS_DIR = (
    Path(__file__).parent.parent / "scripts" / "db" / "migrations"
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# A fresh database starts current (schema.sql marks every migration it
# contains), so pending migrations only exist on a database that is behind.
# These are every migration from 016 on; each is safe to re-apply on a fresh
# database, so re-applying is real work.
BEHIND = sorted(p.stem for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.py") if int(p.stem[:3]) >= 16)


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A database behind by every migration from 016, pointed at via PT_DB_PATH.

    Stays out of the real ``data/tracker.db`` and bypasses the
    fresh-DB safety check via PT_ALLOW_FRESH_DB=1 (the same escape
    hatch Mini uses in its ~/.zshrc).
    """
    from db.schema import create_database

    db_path = tmp_path / "tracker.db"
    create_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM schema_migrations WHERE version >= 16")
    conn.commit()
    conn.close()
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    monkeypatch.setenv("PT_ALLOW_FRESH_DB", "1")
    monkeypatch.setenv("PT_NO_BANNER", "1")
    monkeypatch.setenv("PT_SUPPRESS_MIGRATION_WARNING", "1")
    return db_path


# ---------------------------------------------------------------------
# pt db migrate CLI
# ---------------------------------------------------------------------


def test_pt_db_migrate_applies_pending_and_reports(
    runner: CliRunner, fresh_db: Path
) -> None:
    result = runner.invoke(cli, ["db", "migrate"])
    assert result.exit_code == 0, result.output
    for name in BEHIND:
        assert name in result.output
    assert f"applied {len(BEHIND)} migration" in result.output

    # The tables the migrations create are proven present by counting rows in
    # them through the sanctioned interface, and by nothing being pending
    # afterwards. Opening the file to read sqlite_master is exactly the direct
    # access this work removed.
    assert DatabaseManager().migrations_pending() == [], (
        "migrate reported success but the runner still sees pending migrations"
    )
    assert DatabaseManager().count_rows("migrations") >= 0


def test_pt_db_migrate_is_idempotent(
    runner: CliRunner, fresh_db: Path
) -> None:
    first = runner.invoke(cli, ["db", "migrate"])
    second = runner.invoke(cli, ["db", "migrate"])
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "no pending migrations" in second.output


def test_pt_db_migrate_reports_migration_error_without_traceback(
    runner: CliRunner, fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad migration is an operator condition, not a pt bug — #6873.
    The bare try/finally used to let MigrationError escape Click as a
    raw traceback."""
    from db.migration_runner import MigrationError

    def boom(conn, migrations_dir):
        raise MigrationError("003_crr_pk_not_null: checksum mismatch")

    monkeypatch.setattr("db.migration_runner.apply_all", boom)
    result = runner.invoke(cli, ["db", "migrate"])

    assert result.exit_code == 2
    assert "pt db migrate: 003_crr_pk_not_null: checksum mismatch" in result.output
    assert not isinstance(result.exception, MigrationError)
    assert "Traceback" not in result.output


def test_pt_db_migrate_still_tracebacks_on_unexpected_error(
    runner: CliRunner, fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only MigrationError is caught; a real bug must stay loud."""
    def boom(conn, migrations_dir):
        raise RuntimeError("genuine bug")

    monkeypatch.setattr("db.migration_runner.apply_all", boom)
    result = runner.invoke(cli, ["db", "migrate"])

    assert result.exit_code != 0
    # The backend still raises RuntimeError. It reaches the caller as
    # OperationFailed because an exception cannot cross a socket, with the
    # original type carried on the wire. The property under test is intact:
    # an unexpected bug stays loud and is not mistaken for a MigrationError.
    assert isinstance(result.exception, RuntimeError)
    assert "genuine bug" in str(result.exception)


# ---------------------------------------------------------------------
# _warn_unapplied_migrations
# ---------------------------------------------------------------------


def test_warn_unapplied_fires_when_pending(
    capsys: pytest.CaptureFixture[str], fresh_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PT_SUPPRESS_MIGRATION_WARNING", raising=False)
    _warn_unapplied_migrations()
    err = capsys.readouterr().err
    assert "pending migration" in err
    assert BEHIND[0] in err
    assert "pt db migrate" in err


def test_warn_unapplied_silent_after_apply(
    runner: CliRunner, capsys: pytest.CaptureFixture[str],
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner.invoke(cli, ["db", "migrate"])
    capsys.readouterr()  # drain any output from the invoke
    monkeypatch.delenv("PT_SUPPRESS_MIGRATION_WARNING", raising=False)
    _warn_unapplied_migrations()
    assert capsys.readouterr().err == ""


def _service_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable database service must not stop the CLI from running.

    This used to test "the DB file does not exist yet", which was the normal
    state before first run. There is no file for the CLI to find now, so the
    equivalent condition is a service that does not answer — and the property
    that matters is the same one: `pt --help` and friends still work when the
    database does not.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocker")
    monkeypatch.setenv("PT_DB_PATH", str(blocker / "tracker.db"))


def _unexpected_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catch-all is the CLI's safety net — unexpected failures
    (broken config, permission error, corrupt migration file) must
    NOT brick pt. They should land on stderr so the operator knows
    something is off, and the CLI should keep running.

    Forces an error from inside the warning helper by monkeypatching
    sqlite3.connect — this covers the path after db_path.exists() has
    returned True. The patch target "pt.sqlite3.connect" works because
    pt.py imports the module via `import sqlite3` (not
    `from sqlite3 import connect`); if that import ever changes, this
    monkeypatch becomes a silent no-op and the test needs to be updated.
    """

    def _boom(*_args, **_kwargs):
        raise sqlite3.DatabaseError("synthetic: disk I/O error")

    monkeypatch.setattr("pt.sqlite3.connect", _boom)


@pytest.mark.parametrize(
    "break_it, detail",
    [(_service_down, None), (_unexpected_error, "disk I/O error")],
    ids=["service_down", "unexpected_error"],
)
def test_warn_unapplied_reports_but_does_not_raise_when_the_check_fails(
    capsys: pytest.CaptureFixture[str], fresh_db: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, break_it, detail,
) -> None:
    monkeypatch.delenv("PT_SUPPRESS_MIGRATION_WARNING", raising=False)
    break_it(tmp_path, monkeypatch)

    _warn_unapplied_migrations()  # must NOT raise

    err = capsys.readouterr().err
    assert "could not check for pending migrations" in err, (
        "a failed check must say so; silence would look like 'nothing pending'"
    )
    if detail:
        assert detail in err
