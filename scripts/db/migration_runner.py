"""
Migration runner for project-tracker.

Reads migration modules from ``scripts/db/migrations/``, applies any that
aren't already recorded in the local ``schema_migrations`` ledger, and wraps
each apply in a SQL transaction together with its ledger row, so a failed
migration leaves neither DDL nor a ledger entry behind.

Migration file format
---------------------
Files live in ``scripts/db/migrations/`` and match ``NNN_name.py`` where
``NNN`` is a 3-digit version number. Each module must expose:

- ``up(conn: sqlite3.Connection) -> None`` — applies the migration.
  Called inside an already-open transaction; must **not** issue
  ``BEGIN``, ``COMMIT``, or ``ROLLBACK``.

Older modules also carry a ``CRR_TABLES`` frozenset left over from the
cr-sqlite era (removed by migration 018). The runner ignores it.

Files under ``migrations/`` whose basename doesn't match ``NNN_name.py``
(``__init__.py``, dotfiles, editor backups) are ignored silently. Files
that match the naming pattern but lack ``up()`` emit a stderr warning on
discovery and are skipped. They will never be applied.

A fresh database does not replay these: it is created from schema.sql and
marked for every migration that file contains (see schema.create_database).
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable, cast


MIGRATION_FILENAME_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.py$")


class MigrationError(RuntimeError):
    """Raised when a migration file is malformed."""


@dataclass(frozen=True)
class Migration:
    """One discovered, validated migration ready to apply."""

    version: int
    name: str
    path: Path
    up: Callable[[sqlite3.Connection], None]


def discover_migrations(
    migrations_dir: Path, *, verbose: bool = True
) -> list[Migration]:
    """Return every valid migration in ``migrations_dir``, ordered by version.

    Files that don't match ``NNN_name.py`` are ignored silently. Files
    that match the pattern but fail validation (missing ``up``)
    are skipped — never applied.

    With ``verbose=True`` (default, used from ``pt db migrate``) a
    stderr warning is printed for each skipped file so the operator
    sees authoring bugs. With ``verbose=False`` the same files are
    skipped silently — used from the pt-startup unapplied-migration
    check, where a skip warning on every CLI call would be noise.
    """
    migrations: list[Migration] = []
    seen_versions: dict[int, Path] = {}

    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file():
            continue
        match = MIGRATION_FILENAME_RE.match(path.name)
        if not match:
            continue

        version = int(match.group(1))
        name = match.group(2)

        if version in seen_versions:
            raise MigrationError(
                f"Duplicate migration version {version:03d}: "
                f"{seen_versions[version].name} and {path.name}. "
                "Every migration needs a unique version number."
            )
        seen_versions[version] = path

        try:
            migration = _load_migration_module(version, name, path)
        except MigrationError as err:
            if verbose:
                print(
                    f"migration_runner: skipping {path.name} ({err})",
                    file=sys.stderr,
                )
            continue
        migrations.append(migration)

    migrations.sort(key=lambda m: m.version)
    return migrations


def _load_migration_module(version: int, name: str, path: Path) -> Migration:
    """Import one migration file and validate its exported contract.

    Raises ``MigrationError`` for any contract violation — a missing
    or non-callable ``up``.
    """
    spec = importlib.util.spec_from_file_location(
        f"_pt_migration_{version:03d}", path
    )
    if spec is None or spec.loader is None:
        raise MigrationError(f"cannot load spec for {path.name}")

    module: ModuleType = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise MigrationError(f"import failed: {exc!r}") from exc

    up = getattr(module, "up", None)
    if not callable(up):
        raise MigrationError("missing or non-callable up(conn)")

    return Migration(
        version=version,
        name=name,
        path=path,
        up=cast(Callable[[sqlite3.Connection], None], up),
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already recorded as applied.

    Before the first migration runs the ``schema_migrations`` ledger
    doesn't exist yet — return an empty set so the bootstrap migration
    (the one that creates the ledger) isn't mistaken for already-applied.

    Only the specific "no such table: schema_migrations" case is
    swallowed. Other ``OperationalError`` cases (database locked, disk
    full, corruption) propagate — silencing them would let the runner
    re-attempt every migration and surface as a confusing IntegrityError
    on the PRIMARY KEY instead of the real cause.
    """
    try:
        cur = conn.execute("SELECT version FROM schema_migrations")
    except sqlite3.OperationalError as err:  # governance: allow-silent SF002: only "no such table" returns empty, meaning the ledger does not exist yet so nothing is applied; every other error re-raises
        if "no such table" in str(err).lower():
            return set()
        raise
    return {row[0] for row in cur.fetchall()}


def unapplied_migrations(
    conn: sqlite3.Connection, migrations: Iterable[Migration]
) -> list[Migration]:
    """Filter ``migrations`` down to the ones not yet in ``schema_migrations``.

    Preserves the input order so the caller can apply them sequentially.
    """
    applied = applied_versions(conn)
    return [m for m in migrations if m.version not in applied]


def apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration inside a transaction.

    The sequence is ``BEGIN``, ``migration.up(conn)``, insert the row into
    ``schema_migrations`` (inside the same transaction so "applied" and
    "recorded" commit atomically), ``COMMIT``. On any exception the
    transaction is rolled back, which unwinds the DDL and the ledger insert.
    """
    applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn.execute("BEGIN")
    try:
        migration.up(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (?, ?, ?)",
            (migration.version, migration.name, applied_at),
        )
        conn.execute("COMMIT")
    except Exception:
        # SQLite already rolled back on some errors (disk full, I/O); a second
        # ROLLBACK would raise and hide the error that actually happened.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def apply_all(
    conn: sqlite3.Connection, migrations_dir: Path
) -> list[Migration]:
    """Discover, filter, and apply every pending migration in order.

    Returns the list of migrations that were applied in this call
    (possibly empty). Raises immediately on the first failure — the
    transaction rollback in ``apply_migration`` keeps the DB consistent,
    but subsequent migrations are **not** attempted. The operator fixes
    the failure and re-runs.
    """
    migrations = discover_migrations(migrations_dir)
    pending = unapplied_migrations(conn, migrations)
    for migration in pending:
        apply_migration(conn, migration)
    return pending
