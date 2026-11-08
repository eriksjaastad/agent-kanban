"""pt memory: wraps an optional brain.py memory store (scripts/config.py [memory])."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click
from commands.common import (
    EXIT_BACKEND_UNAVAILABLE,
    EXIT_QUERY_FAILURE,
    EXIT_VALIDATION,
    PT_MEMORY_SCHEMA_VERSION,
    PtJsonError,
    _emit_json,
    _emit_json_error,
)

from scripts.config import MEMORY_NOT_CONFIGURED, local_config_path, memory_integration


def _parse_since_until(value: str | None, option_name: str) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    import re
    from datetime import timedelta, timezone

    match = re.fullmatch(r"(\d+)([dhw])", raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        delta = {
            "d": timedelta(days=amount),
            "h": timedelta(hours=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%d %H:%M:%S")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PtJsonError(
            "validation",
            f"{option_name} must be ISO datetime/date or relative duration like 7d, 12h, 2w",
            EXIT_VALIDATION,
        ) from exc
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _memory_db_path() -> Path | None:
    """The memory store's database: $PT_MEMORY_DB_PATH, else the configured one, else None.

    A local config that can't be read raises PtJsonError, so every command
    reports it in its own error format. Call once per command and pass the
    result on: the file can change while a command runs.
    """
    override = os.environ.get("PT_MEMORY_DB_PATH")
    if override:
        return Path(override).expanduser()
    try:
        integration = memory_integration()
    except ValueError as exc:
        raise PtJsonError("validation", str(exc), EXIT_VALIDATION) from exc
    return integration.brain_db if integration else None


def _require_memory_db_path() -> Path:
    """_memory_db_path(), or backend_unavailable when the memory integration is off."""
    db_path = _memory_db_path()
    if db_path is None:
        raise PtJsonError(
            "backend_unavailable",
            MEMORY_NOT_CONFIGURED.format(path=local_config_path()),
            EXIT_BACKEND_UNAVAILABLE,
        )
    return db_path


def _open_memory_db_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise PtJsonError(
            "backend_unavailable",
            f"memory database not found at {db_path}",
            EXIT_BACKEND_UNAVAILABLE,
        )
    # The memory store owns this source; pt only reads it.
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as exc:
        raise PtJsonError(
            "backend_unavailable",
            f"could not open memory database read-only: {exc}",
            EXIT_BACKEND_UNAVAILABLE,
        ) from exc


def _metadata_dict(row: sqlite3.Row) -> dict:
    raw = row["metadata"]
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:  # governance: allow-silent SF002: a malformed metadata blob in the memory store's read-only rows is treated like a non-dict one; the row and its content are still returned with blank derived fields
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _memory_row_to_dict(row: sqlite3.Row) -> dict:
    metadata = _metadata_dict(row)
    return {
        "id": row["id"],
        "uuid": row["uuid"],
        "content": row["content"],
        "created_at": row["created_at"],
        "source_machine": row["source_machine"] or "",
        "scope": row["scope"] or "",
        "agent_family": row["agent_family"] or metadata.get("agent_family", ""),
        "source_agent": metadata.get("source_agent", ""),
        "project": metadata.get("project", ""),
        "type": metadata.get("type", "observation"),
        "content_type": metadata.get("content_type", ""),
        "metadata": metadata,
    }


def _memory_where_clauses(
    since: str | None,
    until: str | None,
    source: str | None,
    project: str | None,
    query: str | None = None,
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if query:
        clauses.append("(content LIKE ? OR metadata LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at <= ?")
        params.append(until)
    if source:
        clauses.append("(source_machine = ? OR json_extract(metadata, '$.source_agent') = ?)")
        params.extend([source, source])
    if project:
        clauses.append("json_extract(metadata, '$.project') = ?")
        params.append(project)
    return clauses, params


def _memory_query_rows(
    *,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int, str]:


    if limit < 1 or limit > 500:
        raise PtJsonError("validation", "--limit must be between 1 and 500", EXIT_VALIDATION)
    if offset < 0:
        raise PtJsonError("validation", "--offset must be >= 0", EXIT_VALIDATION)
    since_sql = _parse_since_until(since, "--since")
    until_sql = _parse_since_until(until, "--until")
    clauses, params = _memory_where_clauses(since_sql, until_sql, source, project, query)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT id, uuid, content, metadata, scope, agent_family, created_at, source_machine "
        f"FROM thoughts {where_sql} "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    count_sql = f"SELECT COUNT(*) FROM thoughts {where_sql}"
    db_path = _require_memory_db_path()
    try:
        with _open_memory_db_readonly(db_path) as conn:
            total = int(conn.execute(count_sql, params).fetchone()[0])
            rows = conn.execute(sql, [*params, limit, offset]).fetchall()
    except PtJsonError:
        raise
    except sqlite3.Error as exc:
        raise PtJsonError("query_failure", f"memory query failed: {exc}", EXIT_QUERY_FAILURE) from exc
    return [_memory_row_to_dict(row) for row in rows], total, db_path.as_posix()


def _memory_payload(command: str, rows: list[dict], total: int, db_path: str,
                    limit: int, offset: int, filters: dict) -> dict:
    return {
        "schema_version": PT_MEMORY_SCHEMA_VERSION,
        "ok": True,
        "command": command,
        "read_only": True,
        "backend": {
            "type": "sqlite",
            "path": db_path,
        },
        "filters": filters,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
            "total": total,
        },
        "results": rows,
    }


def _run_brain(*args: str) -> None:
    """Call the configured memory store's brain.py with uv run.

    With doppler_project/doppler_config set it runs under ``doppler run`` so
    brain.py gets its own secrets; ``pt`` itself runs without Doppler.
    """
    try:
        integration = memory_integration()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if integration is None:
        raise click.ClickException(MEMORY_NOT_CONFIGURED.format(path=local_config_path()))
    if not integration.brain_py.exists():
        raise click.ClickException(f"brain.py not found at {integration.brain_py}")
    cmd = ["uv", "run", str(integration.brain_py), *args]
    if integration.doppler_project:
        if not shutil.which("doppler"):
            raise click.ClickException("doppler CLI not found. Install with: brew install doppler")
        cmd = ["doppler", "run", "--project", integration.doppler_project,
               "--config", integration.doppler_config, "--", *cmd]
    # No timeout on purpose: `graph build` legitimately runs for most of an hour.
    result = subprocess.run(cmd, check=False, cwd=str(integration.root))
    if result.returncode != 0:
        # brain.py already printed its own error; pass its failure on.
        sys.exit(result.returncode)


@click.group(name="memory", invoke_without_command=True)
@click.pass_context
def memory_group(ctx: click.Context) -> None:
    """Search and write to the shared agent memory store.

    A thin wrapper around the configured store's brain.py so every agent can access
    cross-agent memory without knowing the full path or needing MCP approval.

    \b
    Quick start:
      pt memory search "what did we decide about backups?"
      pt memory write "Decision: use Haiku for routine tasks" --type decision
      pt memory stats

    On success the read-only --json modes (search, recent, stats) print one
    JSON object carrying schema_version, ok and read_only beside its data;
    export prints NDJSON. When those modes or export fail their own checks they exit 2
    (invalid input), 3 (memory backend unavailable) or 4 (query failed).
    Option parsing errors, such as a non-integer --limit, are Click's plain
    usage message with exit 2.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@memory_group.command(name="search")
@click.argument("query_arg", required=False)
@click.option("--query", "query_opt", default=None, help="Search text for JSON/non-interactive mode")
@click.option("--top", "-n", default=5, type=int, help="Number of results (default: 5)")
@click.option("--agent-family", "-a", default="",
              help="Filter to agent family: claude | gemini | codex | antigravity | qwen")
@click.option("--namespace", default="", help="Filter by project prefix (e.g. loop/sales)")
@click.option("--goal-id", default="", help="Filter to loop_state entries for a specific goal ID")
@click.option("--since", default=None, help="Only include rows at/after ISO time or duration like 7d")
@click.option("--until", default=None, help="Only include rows at/before ISO time or duration like 7d")
@click.option("--source", default=None, help="Filter by source_machine or metadata.source_agent")
@click.option("--project", default=None, help="Filter by metadata.project")
@click.option("--limit", default=None, type=int, help="Maximum rows for JSON/non-interactive mode")
@click.option("--offset", default=0, type=int, help="Pagination offset for JSON/non-interactive mode")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def memory_search(query_arg: str | None, query_opt: str | None, top: int, agent_family: str,
                  namespace: str, goal_id: str, since: str | None, until: str | None,
                  source: str | None, project: str | None, limit: int | None,
                  offset: int, json_output: bool) -> None:
    """Search the shared brain by semantic similarity.

    \b
    Examples:
      pt memory search "what was the backup decision?"
      pt memory search "MCP firewall" --top 10
      pt memory search "calendar" --agent-family claude
    """
    query = query_opt or query_arg
    if not query:
        if json_output:
            _emit_json_error(
                PtJsonError("validation", "memory search requires QUERY or --query", EXIT_VALIDATION),
                "memory.search",
            )
        raise click.UsageError("memory search requires QUERY or --query")
    effective_limit = limit if limit is not None else top
    direct_mode = json_output or any([since, until, source, project, limit is not None, offset])
    if direct_mode:
        try:
            rows, total, db_path = _memory_query_rows(
                query=query,
                since=since,
                until=until,
                source=source,
                project=project,
                limit=effective_limit,
                offset=offset,
            )
        except PtJsonError as exc:
            if json_output:
                _emit_json_error(exc, "memory.search")
            raise click.ClickException(exc.message) from exc
        payload = _memory_payload(
            "memory.search",
            rows,
            total,
            db_path,
            effective_limit,
            offset,
            {
                "query": query,
                "since": since,
                "until": until,
                "source": source,
                "project": project,
            },
        )
        if json_output:
            _emit_json(payload)
            return
        for row in rows:
            click.echo(f"#{row['id']} | {row['created_at']} | {row['project']} | {row['type']} | {row['content'][:160]}")
        click.echo(f"\nReturned {len(rows)} of {total}")
        return

    args = ["search", query, "--top", str(top)]
    if agent_family:
        args += ["--agent-family", agent_family]
    if namespace:
        args += ["--namespace", namespace]
    if goal_id:
        args += ["--goal-id", goal_id]
    _run_brain(*args)


@memory_group.command(name="write")
@click.argument("content")
@click.option("--type", "-t", "entry_type", default="observation",
              help="Type: observation | decision | question | insight | error (default: observation)")
@click.option("--project", "-p", default="", help="Project name to tag this entry")
@click.option("--agent-family", "-a", default="",
              help="Agent family: claude | gemini | codex | antigravity | qwen")
@click.option("--scope", "-s", default="",
              help="Scope: shared | agent-scoped (auto-classified if omitted)")
def memory_write(content: str, entry_type: str, project: str, agent_family: str, scope: str) -> None:
    """Write a new entry to the shared brain.

    \b
    Examples:
      pt memory write "Decision: use Haiku for all routine tasks" --type decision
      pt memory write "Bug: pt scan hangs without Discord timeout" --type error --project project-tracker
      pt memory write "Trust layer is the right thesis" --type insight --project data-vault-factory
    """
    # Auto-detect project from cwd when not explicitly provided
    if not project:
        import os
        cwd = os.environ.get("PT_CALLER_CWD", os.getcwd())
        project = os.path.basename(cwd)
    args = ["write", content, "--type", entry_type]
    args += ["--project", project]
    if agent_family:
        args += ["--agent-family", agent_family]
    if scope:
        args += ["--scope", scope]
    _run_brain(*args)


@memory_group.command(name="recent")
@click.option("--since", default=None, help="Only include rows at/after ISO time or duration like 7d")
@click.option("--until", default=None, help="Only include rows at/before ISO time or duration like 7d")
@click.option("--source", default=None, help="Filter by source_machine or metadata.source_agent")
@click.option("--project", default=None, help="Filter by metadata.project")
@click.option("--limit", default=50, type=int, help="Maximum rows")
@click.option("--offset", default=0, type=int, help="Pagination offset")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def memory_recent(since, until, source, project, limit, offset, json_output) -> None:
    """List recent memory entries through a read-only DB connection."""
    try:
        rows, total, db_path = _memory_query_rows(
            since=since,
            until=until,
            source=source,
            project=project,
            limit=limit,
            offset=offset,
        )
    except PtJsonError as exc:
        if json_output:
            _emit_json_error(exc, "memory.recent")
        raise click.ClickException(exc.message) from exc
    payload = _memory_payload(
        "memory.recent",
        rows,
        total,
        db_path,
        limit,
        offset,
        {"since": since, "until": until, "source": source, "project": project},
    )
    if json_output:
        _emit_json(payload)
        return
    for row in rows:
        click.echo(f"#{row['id']} | {row['created_at']} | {row['project']} | {row['type']} | {row['content'][:160]}")
    click.echo(f"\nReturned {len(rows)} of {total}")


@memory_group.command(name="stats")
@click.option("--json", "json_output", is_flag=True, help="Output stable machine-readable JSON")
def memory_stats(json_output) -> None:
    """Show brain database statistics and per-agent search efficiency."""
    if json_output:
        try:
            db_path = _require_memory_db_path()
            with _open_memory_db_readonly(db_path) as conn:
                total = int(conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0])
                by_project = [
                    {"project": row[0] or "", "count": row[1]}
                    for row in conn.execute(
                        "SELECT json_extract(metadata, '$.project') AS project, COUNT(*) "
                        "FROM thoughts GROUP BY project ORDER BY COUNT(*) DESC LIMIT 50"
                    ).fetchall()
                ]
                by_source = [
                    {"source": row[0] or "", "count": row[1]}
                    for row in conn.execute(
                        "SELECT COALESCE(json_extract(metadata, '$.source_agent'), source_machine, '') AS source, COUNT(*) "
                        "FROM thoughts GROUP BY source ORDER BY COUNT(*) DESC LIMIT 50"
                    ).fetchall()
                ]
                bounds = conn.execute("SELECT MIN(created_at), MAX(created_at) FROM thoughts").fetchone()
        except PtJsonError as exc:
            _emit_json_error(exc, "memory.stats")
        except sqlite3.Error as exc:
            _emit_json_error(
                PtJsonError("query_failure", f"memory stats failed: {exc}", EXIT_QUERY_FAILURE),
                "memory.stats",
            )
        _emit_json({
            "schema_version": PT_MEMORY_SCHEMA_VERSION,
            "ok": True,
            "command": "memory.stats",
            "read_only": True,
            "backend": {"type": "sqlite", "path": db_path.as_posix()},
            "stats": {
                "total": total,
                "oldest_created_at": bounds[0],
                "newest_created_at": bounds[1],
                "by_project": by_project,
                "by_source": by_source,
            },
        })
        return
    _run_brain("stats")


@memory_group.command(name="export")
@click.option("--format", "export_format", type=click.Choice(["ndjson"]), default="ndjson")
@click.option("--since", default=None, help="Only include rows at/after ISO time or duration like 7d")
@click.option("--until", default=None, help="Only include rows at/before ISO time or duration like 7d")
@click.option("--source", default=None, help="Filter by source_machine or metadata.source_agent")
@click.option("--project", default=None, help="Filter by metadata.project")
@click.option("--limit", default=500, type=int, help="Maximum rows")
@click.option("--offset", default=0, type=int, help="Pagination offset")
def memory_export(export_format, since, until, source, project, limit, offset) -> None:
    """Export memory rows as newline-delimited JSON for downstream analysis."""
    try:
        rows, _, _ = _memory_query_rows(
            since=since,
            until=until,
            source=source,
            project=project,
            limit=limit,
            offset=offset,
        )
    except PtJsonError as exc:
        _emit_json_error(exc, "memory.export", err=True)
    for row in rows:
        click.echo(json.dumps(row, sort_keys=True))
