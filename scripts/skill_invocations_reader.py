"""Read-only reader for the memory store's ``skill_invocations`` table.

The producer side (the memory store's :mod:`log-skill-invocation` hook, card
#5912) writes one row per Skill tool call into
the configured memory store's brain.db. This module is project-tracker's
consumer: a single read-only sqlite connection, a few filter helpers,
zero writes. No writes anywhere — the reader opens the file with
``mode=ro`` so accidental ``INSERT``/``DELETE`` would raise.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from config import MEMORY_NOT_CONFIGURED, local_config_path, memory_integration
from origin import project_from_cwd

# None means "the configured memory store" (scripts/config.py [memory]); tests set a path.
DEFAULT_BRAIN_DB: Path | None = None


class SkillInvocationsTableMissing(RuntimeError):
    """Raised when the DB file opens but the expected table isn't there."""


@dataclass(frozen=True)
class Invocation:
    """One row from ``skill_invocations``, enriched with a project label."""
    skill_name: str
    cwd: str
    session_id: str
    tool_use_id: str
    caller_type: str
    invoked_at: datetime
    project: str


def _parse_invoked_at(value: str) -> datetime:
    """Parse the ISO-8601 ``invoked_at`` column into an aware datetime.

    The hook writes UTC ISO-8601 (sometimes with ``Z`` suffix); older
    rows may carry the sqlite-default ``CURRENT_TIMESTAMP`` form
    ``YYYY-MM-DD HH:MM:SS`` without a tz. Normalise both to UTC-aware.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@contextmanager
def _open_ro(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``db_path`` read-only via URI mode and yield the connection."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _verify_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_invocations'"
    ).fetchone()
    if row is None:
        raise SkillInvocationsTableMissing(
            "skill_invocations table not found. The memory store's PostToolUse hook "
            "(log-skill-invocation.py, #5912) must have fired at least once to "
            "create it. Check that the hook is registered in ~/.claude/settings.json."
        )


def iter_invocations(
    *,
    since: datetime | None = None,
    skill: str | None = None,
    agent: str | None = None,
    project: str | None = None,
    db_path: Path | None = None,
) -> Iterable[Invocation]:
    """Yield :class:`Invocation` rows matching the given filters.

    Filtering by ``project`` happens in Python (via :func:`project_from_cwd`
    over the ``cwd`` column) because the table doesn't store a project
    column directly. Filtering by ``skill``, ``agent`` (caller_type), and
    ``since`` is pushed down to the WHERE clause. ``agent=""`` matches the
    legitimately-unattributable rows (empty caller_type).

    ``db_path`` defaults to the module-level constant
    resolved *at call time* — tests can monkeypatch those module globals
    before invoking the CLI and have the override take effect.
    """
    if db_path is None:
        db_path = DEFAULT_BRAIN_DB
    if db_path is None:
        integration = memory_integration()
        if integration is None:
            raise FileNotFoundError(MEMORY_NOT_CONFIGURED.format(path=local_config_path()))
        db_path = integration.brain_db
    if not db_path.is_file():
        raise FileNotFoundError(
            f"brain.db not found at {db_path}. Has the memory store been initialised?"
        )

    where: list[str] = []
    params: list[object] = []
    if skill is not None:
        where.append("skill_name = ?")
        params.append(skill)
    if agent is not None:
        where.append("caller_type = ?")
        params.append(agent)
    if since is not None:
        where.append("invoked_at >= ?")
        params.append(since.astimezone(timezone.utc).isoformat(timespec="seconds"))
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    sql = (
        "SELECT skill_name, cwd, session_id, tool_use_id, caller_type, invoked_at "
        "FROM skill_invocations"
        f"{where_sql} "
        "ORDER BY invoked_at DESC"
    )

    with _open_ro(db_path) as conn:
        _verify_table(conn)
        for row in conn.execute(sql, params):
            row_project = project_from_cwd(row["cwd"] or "")
            if project is not None and row_project != project:
                continue
            yield Invocation(
                skill_name=row["skill_name"],
                cwd=row["cwd"] or "",
                session_id=row["session_id"] or "",
                tool_use_id=row["tool_use_id"] or "",
                caller_type=row["caller_type"] or "",
                invoked_at=_parse_invoked_at(row["invoked_at"]),
                project=row_project,
            )
