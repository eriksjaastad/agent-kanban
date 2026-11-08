"""pt migration: snapshot-based recording of bulk operations across a repo (Phase F)."""

import json
import os
import re as _re_migration
import subprocess
import sys
from datetime import datetime
from datetime import timezone as _timezone
from pathlib import Path
from typing import Optional

import click
from commands.common import (
    _LOCAL_DB_ERRORS,
    EXIT_QUERY_FAILURE,
    EXIT_VALIDATION,
    PtJsonError,
    _caller_repo_root,
    _emit_json,
    _emit_json_error,
    _run_git,
)
from commands.handoff import _get_tracker_db
from origin import resolve_created_by as _resolve_created_by

#
# `pt migration start <name>` captures a baseline (git HEAD + porcelain
# status) into a state file at PT_MIGRATION_DIR/<name>.json (default
# ~/.project-tracker/migrations/). `pt migration finish` diffs the current
# working tree against the baseline and PRINTS a manifest (new/modified paths
# plus the header fields). It writes nothing into the repo: an appended
# MIGRATIONS.md made multi-repo sweeps conflict after review had passed.
#
# Optional flags:
#   --commit  marks the session as committed (files left in place)
#   --revert  restores tracked files via `git restore` and trashes
#             untracked files via send2trash (NEVER raw rm)
#
# State file survives reverts because it lives outside the repo. The DB
# row in `migrations` mirrors the lifecycle for cross-session visibility.
# =============================================================================

PT_MIGRATION_SCHEMA_VERSION = "pt.migration.v1"


_MIGRATION_NAME_RE = _re_migration.compile(r"^[A-Za-z0-9_-]{1,60}$")


def _migration_state_dir() -> Path:
    """Resolve the directory holding migration state files.

    Respects PT_MIGRATION_DIR for test isolation; otherwise defaults to
    ~/.project-tracker/migrations/. Creates the directory if missing.
    """
    override = os.environ.get("PT_MIGRATION_DIR")
    if override:
        d = Path(override)
    else:
        d = Path.home() / ".project-tracker" / "migrations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migration_state_path(name: str) -> Path:
    return _migration_state_dir() / f"{name}.json"


def _validate_migration_name(name: str) -> None:
    if not _MIGRATION_NAME_RE.match(name):
        raise PtJsonError(
            "validation",
            f"migration name must match [A-Za-z0-9_-]{{1,60}}, got {name!r}",
            EXIT_VALIDATION,
        )


def _git_head_sha(repo_dir: Path) -> Optional[str]:
    """Return current HEAD SHA, or None if outside a repo / git missing."""
    try:
        stdout, rc = _run_git(repo_dir, ["rev-parse", "HEAD"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):  # governance: allow-silent SF002: documented "HEAD unknown"; it only feeds the advisory HEAD-drift warning, which is skipped when either side is None
        return None
    if rc != 0:
        return None
    sha = stdout.strip()
    return sha or None


def _git_porcelain_baseline(repo_dir: Path) -> list[str]:
    """Return current `git status --porcelain` lines (verbatim) as baseline.

    A git failure raises PtJsonError. An empty baseline at `migration start`
    would make every pre-existing dirty file look new at finish, and
    `--revert` would then restore or trash it.
    """
    try:
        stdout, rc = _run_git(repo_dir, ["status", "--porcelain"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise PtJsonError(
            "query_failure",
            f"git status --porcelain failed in {repo_dir}: {exc}",
            EXIT_QUERY_FAILURE,
        ) from exc
    if rc != 0:
        return []
    return [line for line in stdout.splitlines() if line.strip()]


def _parse_porcelain_paths(lines: list[str]) -> dict[str, str]:
    """Parse porcelain lines into {path: status} maps.

    Returns a dict keyed by current path (the rename target for renames).
    Value is a 2-char XY status; we tag untracked '??' specially.
    """
    out: dict[str, str] = {}
    for line in lines:
        if len(line) < 4:
            continue
        xy = line[:2]
        rest = line[3:].strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        out[rest] = xy
    return out


def _classify_status(xy: str) -> str:
    """Classify a porcelain XY status into {staged, dirty, untracked}."""
    if xy == "??":
        return "untracked"
    x, y = xy[0], xy[1]
    if x in ("A", "M", "R", "C", "D") and y == " ":
        return "staged"
    return "dirty"


def _diff_porcelain(
    baseline: list[str], current: list[str]
) -> tuple[list[dict], list[dict]]:
    """Diff two porcelain snapshots. Returns (added, modified) classified entries.

    `added` — paths present in current but not baseline.
    `modified` — paths present in both but with a different XY status.
    Both lists contain {path, classification, status} dicts.
    """
    base_map = _parse_porcelain_paths(baseline)
    curr_map = _parse_porcelain_paths(current)
    added: list[dict] = []
    modified: list[dict] = []
    for path, xy in curr_map.items():
        if path not in base_map:
            added.append(
                {"path": path, "classification": _classify_status(xy), "status": xy}
            )
        elif base_map[path] != xy:
            modified.append(
                {"path": path, "classification": _classify_status(xy), "status": xy}
            )
    return added, modified


@click.group(name="migration", invoke_without_command=True)
@click.pass_context
def migration_group(ctx: click.Context) -> None:
    """Record bulk-operation sessions (start/finish with manifest)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@migration_group.command(name="start")
@click.argument("name")
@click.option("--force", is_flag=True, help="Overwrite an existing unfinished state file")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def migration_start(name: str, force: bool, json_output: bool) -> None:
    """Open a migration recording session named NAME.

    Captures git HEAD and `git status --porcelain` as the baseline. The
    state file is written to PT_MIGRATION_DIR/<name>.json (default
    ~/.project-tracker/migrations/) so it survives `--revert` and
    `trash` operations on the repo.
    """
    try:
        _validate_migration_name(name)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.start")

    state_path = _migration_state_path(name)
    if state_path.exists() and not force:
        _emit_json_error(
            PtJsonError(
                "validation",
                f"migration {name!r} already has an unfinished state file at "
                f"{state_path}; pass --force to overwrite or run "
                f"`pt migration finish {name}` first",
                EXIT_VALIDATION,
            ),
            "migration.start",
        )

    repo_dir = _caller_repo_root()
    head_sha = _git_head_sha(repo_dir)
    try:
        baseline = _git_porcelain_baseline(repo_dir)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.start")
    now = datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved_created_by = _resolve_created_by()

    state = {
        "schema_version": PT_MIGRATION_SCHEMA_VERSION,
        "name": name,
        "repo_dir": str(repo_dir),
        "started_at": now,
        "baseline_head": head_sha,
        "baseline_porcelain": baseline,
        "created_by": resolved_created_by,
    }
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    # Best-effort DB row (skipped silently if tracker DB unavailable —
    # the state file is the source of truth for revert).
    db_id: Optional[int] = None
    try:
        outcome = _get_tracker_db().migration_session_start(
            name=name,
            started_at=now,
            baseline_head=head_sha,
            created_by=resolved_created_by,
        )
        if outcome["ok"]:
            db_id = outcome["id"]
        else:
            # Best-effort by design: the state file is the source of truth for
            # revert, so a database failure must not abort the command. It used
            # to be `except sqlite3.Error: pass`, which meant the row could
            # vanish with nothing said. Say it.
            click.echo(
                f"⚠ migration session not recorded in the tracker DB: {outcome['error']}",
                err=True,
            )
    except (PtJsonError, *_LOCAL_DB_ERRORS) as exc:
        click.echo(f"⚠ migration session not recorded in the tracker DB: {exc}", err=True)

    if json_output:
        _emit_json({
            "schema_version": PT_MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "command": "migration.start",
            "result": {
                "name": name,
                "state_path": str(state_path),
                "started_at": now,
                "baseline_head": head_sha,
                "baseline_files": len(baseline),
                "db_id": db_id,
            },
        })
    else:
        click.echo(f"✓ Migration {name!r} started at {now}")
        click.echo(f"  state: {state_path}")
        click.echo(f"  HEAD:  {head_sha or '(no HEAD)'}")
        click.echo(f"  baseline: {len(baseline)} dirty/untracked path(s)")


def _build_manifest(
    name: str,
    state: dict,
    finished_at: str,
    added: list[dict],
    modified: list[dict],
    action: str,
    head_drift_warning: Optional[str],
) -> dict:
    """The manifest of a finished migration session, as structured data."""
    return {
        "name": name,
        "started_at": state.get("started_at"),
        "finished_at": finished_at,
        "baseline_head": state.get("baseline_head"),
        "action": action,
        "head_drift_warning": head_drift_warning,
        "added": added,
        "modified": modified,
    }


def _render_manifest(manifest: dict) -> str:
    """Render a `_build_manifest` dict as a markdown section."""
    lines: list[str] = [
        f"## {manifest['name']} — {manifest['finished_at']}",
        "",
        f"- started_at:  `{manifest['started_at']}`",
        f"- finished_at: `{manifest['finished_at']}`",
        f"- baseline_head: `{manifest['baseline_head'] or '(none)'}`",
        f"- action: `{manifest['action']}`",
    ]
    if manifest["head_drift_warning"]:
        lines.append(f"- WARNING: {manifest['head_drift_warning']}")
    for title, entries in (
        ("New paths (introduced during session)", manifest["added"]),
        ("Modified paths (status changed during session)", manifest["modified"]),
    ):
        lines.append("")
        lines.append(f"### {title}")
        if entries:
            for entry in entries:
                lines.append(f"- `[{entry['classification']}]` `{entry['path']}`")
        else:
            lines.append("- _(none)_")
    return "\n".join(lines)


class PathEscapesRepoError(ValueError):
    """A recorded path does not resolve to a location inside its repository."""


def _audit_destructive(operation: str, target: Path, repo_dir: Path) -> None:
    """Record an in-process deletion before it happens.

    The `bash-validator` hook logs destructive *shell* commands, but it cannot
    see a `send2trash()` call inside a running Python process. That blind spot
    is not academic: when a project directory was sent to the Trash while
    its developers were working in it, there was no shell history and no hook
    log to attribute it, because the deletion happened inside `pt` (#6890).

    Anything in this codebase that deletes should record it here first, so the
    next incident is a `grep` rather than an interrogation. Logging must never
    prevent the operation, but a failure is surfaced rather than swallowed —
    a silently broken audit trail is the thing being fixed.
    """
    from datetime import datetime, timezone

    # PT_DESTRUCTIVE_LOG_PATH lets tests redirect this. Without it every test
    # run that touches a delete site writes synthetic pytest-tmp entries into
    # the real log — 51 of 51 entries were test chaff before this existed,
    # which buries genuine evidence in exactly the file meant to preserve it.
    log_path = Path(
        os.environ.get("PT_DESTRUCTIVE_LOG_PATH")
        or Path.home() / ".claude" / "logs" / "pt-destructive.log"
    )
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "target": str(target),
            "repo_dir": str(repo_dir),
            "caller_cwd": os.environ.get("PT_CALLER_CWD", os.getcwd()),
            "pid": os.getpid(),
            "session_id": os.environ.get("CLAUDE_SESSION_ID"),
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 — never block the operation
        print(f"warning: could not write destructive-op audit log: {exc}", file=sys.stderr)


def _contained_path(repo_dir: Path, raw: str) -> Path:
    """Resolve `raw` inside `repo_dir`, refusing anything that escapes it.

    This is a destructive-operation guard, not a formatting nicety. Without it
    `repo_dir / raw` is trivially escapable, because pathlib discards the
    left-hand side when the right-hand side is absolute:

        Path("repo") / "an/absolute/path"  ->  that absolute path, repo discarded
        Path("repo") / ""                  ->  repo   (the root itself)
        Path("repo") / "."                 ->  repo   (the root itself)
        Path("repo") / "../sibling"        ->  repo/../sibling

    A migration revert carrying such a path could send any directory on the
    machine to the Trash from inside an unrelated repo. That is not
    hypothetical — it is what trashed a project directory while its
    developers were working, and it left no shell history and no hook log
    because the deletion happened inside a Python call in another repo's
    process (#6890).

    Resolution is done with `.resolve()` on both sides so symlinked paths
    cannot be used to step outside either.

    Raises PathEscapesRepoError for absolute paths, `..` escapes, empty/`.`
    paths that name the repository root itself, and symlink escapes.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathEscapesRepoError("path is empty")

    # A NUL byte makes `.resolve()`'s lstat raise a bare ValueError, which would
    # escape the PathEscapesRepoError handlers and abort the whole revert
    # instead of skipping just this entry. Refuse it as what it is.
    if "\x00" in raw:
        raise PathEscapesRepoError(f"path contains a NUL byte: {raw!r}")

    candidate = Path(raw)
    if candidate.is_absolute():
        raise PathEscapesRepoError(f"path is absolute: {raw!r}")

    root = repo_dir.resolve()
    try:
        target = (root / candidate).resolve()
    except (ValueError, OSError) as exc:
        # Anything else the filesystem refuses to resolve is, by definition,
        # not a path we can prove is inside the repo.
        raise PathEscapesRepoError(f"path could not be resolved: {raw!r} ({exc})") from exc

    if target == root:
        raise PathEscapesRepoError(f"path resolves to the repository root: {raw!r}")
    if not target.is_relative_to(root):
        raise PathEscapesRepoError(f"path escapes the repository: {raw!r}")
    return target


def _revert_paths(
    repo_dir: Path, entries: list[dict]
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """Revert paths discovered between start and finish.

    Tracked paths (classification staged/dirty) → `git restore --` per path.
    Untracked paths → send2trash. Already-missing untracked files are
    reported as "already missing" rather than erroring.

    Every path is checked with `_contained_path` before anything destructive
    happens. State files written before that guard existed may carry absolute
    or escaping paths, so validating only at the `--files` entry point is not
    enough — the check has to sit at the destructive boundary too.

    Returns (restored, trashed, errors) where errors is [(path, reason)].
    """
    restored: list[str] = []
    trashed: list[str] = []
    errors: list[tuple[str, str]] = []

    def _safe(entries_in: list[dict]) -> list[dict]:
        """Drop entries that escape the repo, recording why."""
        kept = []
        for entry in entries_in:
            try:
                _contained_path(repo_dir, entry["path"])
            except PathEscapesRepoError as exc:
                errors.append((str(entry.get("path")), f"refused: {exc}"))
                continue
            kept.append(entry)
        return kept

    tracked = _safe([e for e in entries if e["classification"] in ("staged", "dirty")])
    untracked = _safe([e for e in entries if e["classification"] == "untracked"])

    # Restore tracked files (one-shot — git restore takes a list).
    if tracked:
        paths = [e["path"] for e in tracked]
        try:
            stdout, rc = _run_git(repo_dir, ["restore", "--"] + paths)
            if rc == 0:
                restored.extend(paths)
            else:
                # Best-effort per-path fallback so one bad path doesn't
                # block the others.
                for p in paths:
                    _, rc1 = _run_git(repo_dir, ["restore", "--", p])
                    if rc1 == 0:
                        restored.append(p)
                    else:
                        errors.append((p, f"git restore exit {rc1}"))
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            errors.extend((p, f"git restore failed: {exc}") for p in paths)

    # Trash untracked files via send2trash (NEVER raw rm).
    if untracked:
        try:
            from send2trash import send2trash  # type: ignore
        except ImportError as exc:
            for e in untracked:
                errors.append((e["path"], f"send2trash unavailable: {exc}"))
            return restored, trashed, errors

        for e in untracked:
            # Re-derive through the guard rather than reusing repo_dir / path:
            # this is the line that actually deletes, so it resolves its own
            # target rather than trusting an earlier check (#6890).
            try:
                target = _contained_path(repo_dir, e["path"])
            except PathEscapesRepoError as exc:
                errors.append((str(e.get("path")), f"refused: {exc}"))
                continue
            if not target.exists():
                errors.append((e["path"], "already missing"))
                continue
            try:
                _audit_destructive("send2trash", target, repo_dir)
                send2trash(str(target))
                trashed.append(e["path"])
            except Exception as exc:  # noqa: BLE001 — trash backend can raise OSError variants
                errors.append((e["path"], f"send2trash failed: {exc}"))

    return restored, trashed, errors


@migration_group.command(name="finish")
@click.argument("name")
@click.option("--commit", "commit_flag", is_flag=True, help="Mark session as committed (leaves files in place)")
@click.option("--revert", "revert_flag", is_flag=True, help="Restore tracked files; trash untracked files")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def migration_finish(
    name: str, commit_flag: bool, revert_flag: bool, json_output: bool
) -> None:


    """Close migration NAME and print its manifest of touched paths.

    The manifest is printed (or, with --json, returned under "manifest");
    nothing is written into the repo. Without --commit or --revert, files
    are left in place. --commit and --revert are mutually exclusive.
    """
    try:
        _validate_migration_name(name)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.finish")

    if commit_flag and revert_flag:
        _emit_json_error(
            PtJsonError(
                "validation",
                "--commit and --revert are mutually exclusive",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    state_path = _migration_state_path(name)
    if not state_path.exists():
        _emit_json_error(
            PtJsonError(
                "validation",
                f"no state file for migration {name!r} at {state_path}; "
                f"run `pt migration start {name}` first",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _emit_json_error(
            PtJsonError(
                "validation",
                f"state file at {state_path} is unreadable: {exc}",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    caller_root = _caller_repo_root()
    repo_dir = Path(state.get("repo_dir") or caller_root)

    # Every state file written before #6851 carries project-tracker as its
    # repo_dir, whatever repo the operator was actually in. Today that is
    # mostly benign — baseline and current were both computed against the
    # wrong tree, so the diff comes out empty — but `--revert` runs
    # `git restore` with cwd=repo_dir and trashes `repo_dir / path`. Acting
    # on a stale state file would be a destructive operation against the
    # wrong worktree, so refuse rather than guess which one is right.
    if revert_flag and repo_dir.resolve() != caller_root.resolve():
        _emit_json_error(
            PtJsonError(
                "validation",
                f"refusing --revert: state file records repo_dir {repo_dir}, "
                f"but this session's repo root is {caller_root}. Reverting "
                f"would run `git restore` and trash files in a different tree. "
                f"Re-run `pt migration finish {name}` from {repo_dir}, or "
                f"finish without --revert and undo by hand.",
                EXIT_VALIDATION,
            ),
            "migration.finish",
        )

    baseline_head: Optional[str] = state.get("baseline_head")
    current_head = _git_head_sha(repo_dir)
    head_drift_warning: Optional[str] = None
    if baseline_head and current_head and current_head != baseline_head:
        head_drift_warning = (
            f"HEAD drifted from baseline {baseline_head[:12]} "
            f"to {current_head[:12]}; revert will restore against current HEAD"
        )

    baseline = state.get("baseline_porcelain", []) or []
    try:
        current = _git_porcelain_baseline(repo_dir)
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.finish")
    added, modified = _diff_porcelain(baseline, current)

    finished_at = datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Decide action label.
    if revert_flag:
        action = "reverted"
    elif commit_flag:
        action = "committed"
    else:
        action = "manifest-only"

    revert_summary: dict = {"restored": [], "trashed": [], "errors": []}
    if revert_flag:
        # Revert both added (new in session) and modified (changed in session).
        all_entries = added + modified
        restored, trashed, errors = _revert_paths(repo_dir, all_entries)
        revert_summary = {
            "restored": restored,
            "trashed": trashed,
            "errors": [{"path": p, "reason": r} for p, r in errors],
        }

    manifest = _build_manifest(
        name, state, finished_at, added, modified, action, head_drift_warning
    )

    # Update DB row if present.
    status_value = {
        "manifest-only": "finished",
        "committed": "committed",
        "reverted": "reverted",
    }[action]
    try:
        outcome = _get_tracker_db().migration_session_finish(
            name=name,
            finished_at=finished_at,
            status=status_value,
        )
        if not outcome["ok"]:
            # Best-effort, but not silent — see migration start.
            click.echo(
                f"⚠ migration session not closed in the tracker DB: {outcome['error']}",
                err=True,
            )
    except (PtJsonError, *_LOCAL_DB_ERRORS) as exc:
        click.echo(f"⚠ migration session not closed in the tracker DB: {exc}", err=True)

    # Remove the state file so the name is reusable.
    try:
        state_path.unlink()  # governance: allow-delete DS001: pt's own state file from `migration start`; its content is in the printed manifest
    except OSError as exc:
        # The session is finished and its manifest printed, but a leftover state
        # file would let a second `finish` replay the stale baseline. Say so.
        click.echo(f"⚠ could not remove migration state file {state_path}: {exc}", err=True)

    if json_output:
        _emit_json({
            "schema_version": PT_MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "command": "migration.finish",
            "result": {
                "name": name,
                "finished_at": finished_at,
                "action": action,
                "manifest": manifest,
                "added": added,
                "modified": modified,
                "head_drift_warning": head_drift_warning,
                "revert_summary": revert_summary,
            },
        })
        return

    click.echo(f"✓ Migration {name!r} finished at {finished_at}")
    click.echo(f"  action:   {action}")
    click.echo(f"  added:    {len(added)}  modified: {len(modified)}")
    if head_drift_warning:
        click.echo(f"  WARNING: {head_drift_warning}")
    if revert_flag:
        click.echo(f"  restored: {len(revert_summary['restored'])} tracked path(s)")
        click.echo(f"  trashed:  {len(revert_summary['trashed'])} untracked path(s)")
        if revert_summary["errors"]:
            click.echo(f"  errors:   {len(revert_summary['errors'])}")
            for e in revert_summary["errors"]:
                click.echo(f"    - {e['path']}: {e['reason']}")
    click.echo("")
    click.echo(_render_manifest(manifest))


@migration_group.command(name="list")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON envelope")
def migration_list(json_output: bool) -> None:
    """List migration sessions recorded in the tracker DB."""
    try:
        db = _get_tracker_db()
    except PtJsonError as exc:
        _emit_json_error(exc, "migration.list")

    try:
        records = db.migration_session_list()
    except _LOCAL_DB_ERRORS as exc:
        _emit_json_error(
            PtJsonError("query_failure", f"migration list failed: {exc}", EXIT_QUERY_FAILURE),
            "migration.list",
        )

    if json_output:
        _emit_json({
            "schema_version": PT_MIGRATION_SCHEMA_VERSION,
            "ok": True,
            "command": "migration.list",
            "result": records,
        })
        return

    if not records:
        click.echo("No migrations recorded.")
        return
    for r in records:
        finished = r["finished_at"] or "open"
        click.echo(
            f"#{r['id']:>3} {r['name']:<30} {r['status']:<10} "
            f"started={r['started_at']} finished={finished}"
        )
