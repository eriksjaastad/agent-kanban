"""Tests for scripts/db/migration_runner.py.

The runner owns the ``schema_migrations`` ledger. These tests pin its
contract:

- discovery: tolerant of non-migration files, strict about malformed
  migrations that *claim* to be migrations (no ``up``);
- applied-versions: returns an empty set before the bootstrap
  migration runs (so the bootstrap isn't mistaken for already-applied);
- apply_migration: runs inside a transaction, records the ledger row
  atomically with the DDL, and rolls the whole thing back on failure
  — including the ledger insert, so the runner will retry the failed
  migration on the next call;
- apply_all: idempotent (re-running is a no-op), applies pending
  migrations in version order, stops at the first failure.

Tests write their own synthetic migration files into a tmp directory so
nothing here depends on the real ``scripts/db/migrations/`` tree.
"""

from __future__ import annotations

import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.migration_runner import (  # noqa: E402 — path setup precedes import
    MigrationError,
    applied_versions,
    apply_all,
    apply_migration,
    discover_migrations,
    unapplied_migrations,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def _valid_migration(version: int, name: str) -> str:
    """Minimal valid migration body — trivial DDL."""
    return f"""
    from __future__ import annotations

    import sqlite3

    def up(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE {name}_{version:03d} (id INTEGER PRIMARY KEY)"
        )
    """


def _ledger_conn() -> sqlite3.Connection:
    """Fresh in-memory DB with the ``schema_migrations`` table pre-created."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    return conn


# ---------------------------------------------------------------------
# discover_migrations
# ---------------------------------------------------------------------


def test_discover_empty_dir(tmp_path: Path) -> None:
    assert discover_migrations(tmp_path) == []


def test_discover_returns_migrations_in_version_order(tmp_path: Path) -> None:
    _write(tmp_path / "010_late.py", _valid_migration(10, "late"))
    _write(tmp_path / "002_early.py", _valid_migration(2, "early"))
    _write(tmp_path / "005_middle.py", _valid_migration(5, "middle"))

    migrations = discover_migrations(tmp_path)
    assert [m.version for m in migrations] == [2, 5, 10]
    assert [m.name for m in migrations] == ["early", "middle", "late"]


def test_discover_ignores_non_matching_filenames(tmp_path: Path) -> None:
    """__init__.py, dotfiles, editor swap files: silently ignored."""
    (tmp_path / "__init__.py").write_text("# package marker\n")
    (tmp_path / ".DS_Store").write_text("")
    (tmp_path / "README.md").write_text("# notes\n")
    (tmp_path / "002_real.py.swp").write_text("editor swap\n")

    _write(tmp_path / "001_real.py", _valid_migration(1, "real"))

    migrations = discover_migrations(tmp_path)
    assert [m.version for m in migrations] == [1]


def test_discover_rejects_duplicate_versions(tmp_path: Path) -> None:
    _write(tmp_path / "003_one.py", _valid_migration(3, "one"))
    _write(tmp_path / "003_two.py", _valid_migration(3, "two"))

    with pytest.raises(MigrationError, match="Duplicate migration version 003"):
        discover_migrations(tmp_path)


def test_discover_skips_file_without_up(tmp_path: Path, capsys) -> None:
    """Matches ``NNN_name.py`` but has no ``up`` — legacy/deprecated
    artifact. Skip with a stderr warning, don't raise."""
    _write(
        tmp_path / "001_legacy.py",
        """
        # No up() — this is the shape of the deprecated 001 script.
        """,
    )
    _write(tmp_path / "002_real.py", _valid_migration(2, "real"))

    migrations = discover_migrations(tmp_path)
    assert [m.version for m in migrations] == [2]

    err = capsys.readouterr().err
    assert "001_legacy.py" in err
    assert "up" in err


def test_discover_ignores_legacy_crr_tables_declaration(tmp_path: Path) -> None:
    """Migrations 002-017 still carry a CRR_TABLES frozenset from the
    cr-sqlite era; the runner neither requires nor validates it."""
    _write(
        tmp_path / "003_legacy_decl.py",
        """
        CRR_TABLES = frozenset({"not_a_table_anywhere"})

        def up(conn):
            pass
        """,
    )
    _write(tmp_path / "004_no_decl.py", _valid_migration(4, "no_decl"))
    assert [m.version for m in discover_migrations(tmp_path)] == [3, 4]


# ---------------------------------------------------------------------
# applied_versions / unapplied_migrations
# ---------------------------------------------------------------------


def test_applied_versions_is_empty_when_ledger_missing() -> None:
    """Before the bootstrap migration runs, the ledger table doesn't
    exist. Returning an empty set is what makes the bootstrap migration
    runnable — otherwise the runner couldn't decide whether to apply
    the migration that creates its own ledger."""
    conn = sqlite3.connect(":memory:")
    assert applied_versions(conn) == set()


def test_applied_versions_reraises_non_missing_table_errors() -> None:
    """The empty-set return is narrow: only swallow 'no such table'.
    Any other OperationalError (locked, disk full, corruption)
    propagates so the caller sees the real failure instead of a
    confusing IntegrityError at insert time."""
    conn = _ledger_conn()

    class _Boom:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        applied_versions(_Boom())  # type: ignore[arg-type]

    # Sanity: the real conn still works.
    assert applied_versions(conn) == set()


def test_applied_versions_reflects_ledger_rows() -> None:
    conn = _ledger_conn()
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) "
        "VALUES (2, 'a', '2026-01-01T00:00:00+00:00'), "
        "       (5, 'b', '2026-01-02T00:00:00+00:00')"
    )
    assert applied_versions(conn) == {2, 5}


def test_unapplied_filters_out_recorded_versions(tmp_path: Path) -> None:
    _write(tmp_path / "001_a.py", _valid_migration(1, "a"))
    _write(tmp_path / "002_b.py", _valid_migration(2, "b"))
    _write(tmp_path / "003_c.py", _valid_migration(3, "c"))
    migrations = discover_migrations(tmp_path)

    conn = _ledger_conn()
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at) "
        "VALUES (1, 'a', '2026-01-01T00:00:00+00:00')"
    )
    pending = unapplied_migrations(conn, migrations)
    assert [m.version for m in pending] == [2, 3]


# ---------------------------------------------------------------------
# apply_migration
# ---------------------------------------------------------------------


def test_apply_migration_runs_up_and_records_ledger_row(tmp_path: Path) -> None:
    _write(tmp_path / "001_bootstrap.py", _valid_migration(1, "bootstrap"))
    [migration] = discover_migrations(tmp_path)

    conn = _ledger_conn()
    apply_migration(conn, migration)

    assert applied_versions(conn) == {1}
    row = conn.execute(
        "SELECT version, name FROM schema_migrations WHERE version = 1"
    ).fetchone()
    assert row == (1, "bootstrap")

    # The migration's own DDL also ran.
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='bootstrap_001'"
    )
    assert cur.fetchone() is not None


def test_apply_migration_creates_bootstrap_ledger_in_same_transaction(
    tmp_path: Path,
) -> None:
    """The real ``002_add_sync_ledgers`` migration creates the
    ``schema_migrations`` table, then the runner inserts into it in the
    same transaction. This exercises that path with a synthetic
    bootstrap migration — if the commit order is wrong (insert before
    CREATE), the test fails."""
    _write(
        tmp_path / "002_bootstrap_ledger.py",
        """
        from __future__ import annotations
        import sqlite3

        CRR_TABLES = frozenset()

        def up(conn: sqlite3.Connection) -> None:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "applied_at TEXT NOT NULL)"
            )
        """,
    )
    [migration] = discover_migrations(tmp_path)

    conn = sqlite3.connect(":memory:")  # ledger does NOT exist yet
    assert applied_versions(conn) == set()

    apply_migration(conn, migration)
    assert applied_versions(conn) == {2}


def test_apply_migration_rolls_back_ddl_and_ledger_on_failure(
    tmp_path: Path,
) -> None:
    """Runner guarantees atomicity: a failing ``up`` leaves no trace —
    not the partial DDL, not a ledger row. Re-running must retry."""
    _write(
        tmp_path / "001_fails_halfway.py",
        """
        from __future__ import annotations
        import sqlite3

        CRR_TABLES = frozenset()

        def up(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE thing_a (id INTEGER PRIMARY KEY)")
            # Fails here — ROLLBACK must unwind thing_a too.
            conn.execute("INVALID SQL STATEMENT")
        """,
    )
    [migration] = discover_migrations(tmp_path)

    conn = _ledger_conn()
    with pytest.raises(sqlite3.OperationalError):
        apply_migration(conn, migration)

    # Ledger is empty — migration did NOT record itself.
    assert applied_versions(conn) == set()

    # DDL was unwound — thing_a does not exist.
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='thing_a'"
    )
    assert cur.fetchone() is None


# ---------------------------------------------------------------------
# apply_all
# ---------------------------------------------------------------------


def test_apply_all_applies_pending_in_order(tmp_path: Path) -> None:
    _write(tmp_path / "001_a.py", _valid_migration(1, "a"))
    _write(tmp_path / "002_b.py", _valid_migration(2, "b"))
    _write(tmp_path / "003_c.py", _valid_migration(3, "c"))

    conn = _ledger_conn()
    applied = apply_all(conn, tmp_path)

    assert [m.version for m in applied] == [1, 2, 3]
    assert applied_versions(conn) == {1, 2, 3}


def test_apply_all_is_idempotent(tmp_path: Path) -> None:
    _write(tmp_path / "001_a.py", _valid_migration(1, "a"))
    _write(tmp_path / "002_b.py", _valid_migration(2, "b"))

    conn = _ledger_conn()
    first = apply_all(conn, tmp_path)
    second = apply_all(conn, tmp_path)

    assert [m.version for m in first] == [1, 2]
    assert second == []  # nothing left to apply


def test_apply_all_stops_at_first_failure_and_preserves_prior_state(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "001_ok.py", _valid_migration(1, "ok"))
    _write(
        tmp_path / "002_broken.py",
        """
        from __future__ import annotations
        import sqlite3

        CRR_TABLES = frozenset()

        def up(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE before_error (id INTEGER PRIMARY KEY)")
            conn.execute("NOT VALID SQL")
        """,
    )
    _write(tmp_path / "003_never_runs.py", _valid_migration(3, "never_runs"))

    conn = _ledger_conn()
    with pytest.raises(sqlite3.OperationalError):
        apply_all(conn, tmp_path)

    # 001 committed; 002 rolled back; 003 never attempted.
    assert applied_versions(conn) == {1}
    cur = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name IN ('before_error', 'never_runs_003', 'ok_001')"
    )
    names = {r[0] for r in cur.fetchall()}
    assert "ok_001" in names
    assert "before_error" not in names
    assert "never_runs_003" not in names


# ---------------------------------------------------------------------
# Real 002 migration
# ---------------------------------------------------------------------


def test_real_002_migration_creates_both_ledgers() -> None:
    """Exercise the actual migration file this PR ships — not a mock."""
    migrations_dir = Path(__file__).parent.parent / "scripts" / "db" / "migrations"
    real_002 = next(
        m for m in discover_migrations(migrations_dir) if m.version == 2
    )
    assert real_002.name == "add_sync_ledgers"

    conn = sqlite3.connect(":memory:")  # no ledger yet — cold start
    apply_migration(conn, real_002)

    # Both tables exist.
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "schema_migrations" in tables
    assert "schema_migration_announcements" in tables

    # Ledger recorded this migration as applied.
    assert applied_versions(conn) == {2}
