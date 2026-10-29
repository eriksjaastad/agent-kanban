"""Maintain scripts/db/schema.sql, the schema every fresh database starts from.

schema.sql is the base schema itself: nothing can rebuild it from scratch,
because the early migrations only alter tables an older schema created. It
changes only through this script. `make schema` (or `--write`) builds a
throwaway database from the committed file through `create_database`,
applies every migration newer than its header, and writes the result back,
with the last migration it contains and a hash of its body in the header.
`create_database` marks the ledger up to that version, so a fresh database
never replays migrations it already has.

Run it after adding a migration. `--check` (used by the test suite) fails
when a migration is not folded in yet or when the body no longer matches its
hash, i.e. schema.sql was edited by hand. Both modes refuse a hand-edited
file: change the schema with a migration instead.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.migration_runner import apply_all
from db.schema import MIGRATIONS_DIR, SCHEMA_SQL, create_database

HEADER = "-- Maintained by scripts/db/schema_dump.py (make schema). Never edit by hand: add a migration.\n"
VERSION_PREFIX = "-- Migrations: through "
HASH_PREFIX = "-- Body sha256: "

# Ledgers nothing reads any more; existing databases keep them, new ones don't
# get them. `_metadata` holds per-database values (pt.machine_id), so its rows
# are never seed data.
_SKIPPED_TABLES = frozenset({"schema_version", "schema_migration_announcements"})
_NO_SEED_ROWS = frozenset({"schema_migrations", "_metadata"})
_OBJECT_ORDER = "CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'trigger' THEN 2 ELSE 3 END"


def dump(conn: sqlite3.Connection) -> str:
    """schema.sql text for the database on ``conn``."""
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    objects = conn.execute(
        f"""SELECT type, name, sql FROM sqlite_master
            WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
            ORDER BY {_OBJECT_ORDER}, name"""
    ).fetchall()
    parts = []
    tables = []
    for kind, name, sql in objects:
        owner = name if kind == "table" else conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()[0]
        if owner in _SKIPPED_TABLES:
            continue
        parts.append(f"\n{sql.strip()};\n")
        if kind == "table":
            tables.append(name)
    for table in tables:
        if table in _NO_SEED_ROWS:
            continue
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall():
            values = ", ".join(conn.execute("SELECT quote(?)", (value,)).fetchone()[0] for value in row)
            parts.append(f'\nINSERT INTO "{table}" VALUES ({values});\n')
    body = "".join(parts)
    return f"{HEADER}{VERSION_PREFIX}{version:03d}\n{HASH_PREFIX}{_digest(body)}\n{body}"


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def hand_edited(text: str) -> bool:
    """True when schema.sql's body no longer matches the hash in its header."""
    _, _, body = text.partition(f"\n{HASH_PREFIX}")
    recorded, _, body = body.partition("\n")
    return not recorded or recorded != _digest(body)


def build() -> str:
    """Build a database through the normal path plus pending migrations; dump it."""
    with tempfile.TemporaryDirectory(prefix="pt-schema-") as tmp:
        path = Path(tmp) / "tracker.db"
        create_database(path)
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            apply_all(conn, MIGRATIONS_DIR)
            return dump(conn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or check scripts/db/schema.sql.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="rewrite scripts/db/schema.sql")
    mode.add_argument("--check", action="store_true", help="exit 1 if schema.sql is out of date")
    args = parser.parse_args(argv)

    if hand_edited(SCHEMA_SQL.read_text()):
        print(f"{SCHEMA_SQL} was edited by hand: restore it from git and add a migration instead",
              file=sys.stderr)
        return 1
    generated = build()
    if args.write:
        SCHEMA_SQL.write_text(generated)
        print(f"wrote {SCHEMA_SQL}")
        return 0
    if SCHEMA_SQL.exists() and SCHEMA_SQL.read_text() == generated:
        return 0
    print(f"{SCHEMA_SQL} is out of date: run `make schema` and commit it", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
