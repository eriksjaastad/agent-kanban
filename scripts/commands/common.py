"""Helpers every pt command group shares: output, exit codes, JSON errors, card display."""

import json
import os
import shlex
import sqlite3
import subprocess
from pathlib import Path
from typing import List, NoReturn, Optional

import click
from db.checkouts import MACHINE_LABELS
from db.schema import FreshDatabaseError, SafetyError
from rich.console import Console
from rich.markup import escape

# Local operations expose validation and safety errors directly, without RPC wrapping.
_LOCAL_DB_ERRORS = (
    sqlite3.Error, OSError, RuntimeError, ValueError,
    SafetyError, FreshDatabaseError,
)


console = Console()


PT_VERSION = "0.0.0"


PT_JSON_SCHEMA_VERSION = "pt.v1"


PT_MEMORY_SCHEMA_VERSION = "pt.memory.v1"


PT_CONFIG_SCHEMA_VERSION = "pt.config.v1"


PT_DOCTOR_SCHEMA_VERSION = "pt.doctor.v1"


PT_HYGIENE_SCHEMA_VERSION = "pt.hygiene.v2"


EXIT_VALIDATION = 2


EXIT_BACKEND_UNAVAILABLE = 3


EXIT_QUERY_FAILURE = 4


EXIT_AUTH = 5


EXIT_HYGIENE_FINDING = 6


def _has_complete_prompt(prompt):
    if not prompt: return False
    prompt_lower = prompt.lower()
    return "## overview" in prompt_lower and "## execution" in prompt_lower and "## done criteria" in prompt_lower


def _coerce_display_map(display_map) -> dict:
    """JSON-RPC may stringify snowflake dict keys; coerce to int."""
    if not display_map:
        return {}
    return {int(k): v for k, v in display_map.items()}


def _display_id_map(db, task_ids) -> dict:
    """{snowflake_pk: display_id} for the given ids (empty if no db/ids)."""
    ids = [i for i in task_ids if i is not None]
    if not db or not ids:
        return {}
    return _coerce_display_map(db.get_task_display_id_map(ids))


def _format_task_ref(task_id, display_map=None, db=None) -> str:
    """Human card number: prefer 4-digit display_id over snowflake pk."""
    if display_map is None and db is not None:
        display_map = _display_id_map(db, [task_id])
    display_map = display_map or {}
    label = display_map.get(int(task_id), task_id) if task_id is not None else task_id
    return f"#{label}"


def _format_task_refs(task_ids, db=None) -> str:
    """Comma-separated human card refs for a list of snowflake pks."""
    ids = list(task_ids or [])
    display_map = _display_id_map(db, ids)
    return ", ".join(_format_task_ref(i, display_map=display_map) for i in ids)


def _blocked_state(db, task_id) -> tuple:
    """(is_blocked, blocking_ids, reason) for one card, failing closed (#6900).

    An unreadable blocked_by, or a failed blocker lookup, counts as BLOCKED
    and carries a reason instead of ids. `pt tasks start` refuses with that
    reason; listings and the tree still render the card, marked blocked.
    """
    try:
        is_blocked, blocking_ids = db.is_blocked(task_id)
        if is_blocked and not blocking_ids:
            return True, [], db.blocked_by_error(task_id) or "blocked_by unreadable"
    except Exception as exc:  # noqa: BLE001 - fail closed; never render as unblocked
        return True, [], f"blocked state unreadable: {exc}"
    return is_blocked, list(blocking_ids), None


def _blocked_label(blocking_ids, reason, display_map) -> str:
    """Text after `[B:` in listings: blocker card numbers, or `unreadable`."""
    if reason:
        return "unreadable"
    return ",".join(str(display_map.get(int(i), i)) for i in blocking_ids)


def _checkout_label(checkout: dict) -> str:
    """`CHECKED OUT — Mac Mini (Qwen on Mini, since 2026-10-16)`, the one wording every surface prints."""
    return (
        f"CHECKED OUT — {MACHINE_LABELS[checkout['machine']]} "
        f"({checkout['holder']}, since {checkout['checked_out_at'][:10]})"
    )


def _checkout_marker(checkouts: dict, project_id: str) -> str:
    """`[OUT: Mac Mini] ` for a card whose project is checked out, else empty."""
    checkout = checkouts.get(project_id)
    return f"[OUT: {MACHINE_LABELS[checkout['machine']]}] " if checkout else ""


def _print_checkin_reminders(db, reminders: list) -> None:
    """One line per checkout due for check-in (#8626), so its holder sees it."""
    for due in reminders:
        # --checkout-id, so a stale reminder can't close a newer checkout of the same project.
        # Options, then `--`, then the id, so an id like -x or --help is never read as an option;
        # shlex.join, so one like r&d reaches the CLI as one argument.
        command = shlex.join(["pt", "project", "checkin", "--checkout-id", str(due["id"]), "--by", "<you>",
                              "--", due["project_id"]])
        console.print(
            f"[bold yellow]Check in {escape(due['project_id'])}: out to {MACHINE_LABELS[due['machine']]} "
            f"({escape(due['holder'])}) for {_format_task_ref(due['task_id'], db=db)}, and {due['reason']}. "
            f"Run: {escape(command)}[/bold yellow]",
            soft_wrap=True,  # one line, so the command can be copied and grepped
            emoji=False,  # or a project id like :smile: prints as an emoji
        )


def _display_tasks(task_list, project=None, json_output=False, db=None, checkouts=None):
    import json as json_lib
    checkouts = checkouts or {}
    if json_output:
        for task in task_list:
            task["checkout"] = checkouts.get(task["project_id"])
        backend = "local"
        print(json_lib.dumps({"tasks": task_list, "total": len(task_list), "backend": backend}, indent=2))
        return
    if not task_list:
        filter_msg = f" for project '{project}'" if project else ""
        print(f"No tasks found{filter_msg}.")
        return
    backend_tag = "local"
    title = f"Tasks - {project}" if project else "Tasks - all projects"
    print(f"{title} [{backend_tag}]\n")
    # Include blocker ids in the display map so [B:…] stays four-digit friendly.
    map_ids = [t["id"] for t in task_list]
    blocked_states = {}
    if db:
        for task in task_list:
            if task.get("blocked_by"):
                state = _blocked_state(db, task["id"])
                blocked_states[task["id"]] = state
                if state[0]:
                    map_ids.extend(state[1])
    display_map = _display_id_map(db, map_ids)
    for task in task_list:
        priority = task.get("priority") or "-"
        status = task["status"]
        task_id = task["id"]
        task_text = task["text"]
        label = display_map.get(int(task_id), task_id)
        if _has_complete_prompt(task.get("prompt")): prompt_marker = ""
        elif task.get("prompt"): prompt_marker = "[~P] "
        else: prompt_marker = "[!P] "
        blocked_marker = ""
        if db and task.get("blocked_by"):
            is_blocked, blocking_ids, reason = blocked_states[task_id]
            if is_blocked:
                b_labels = _blocked_label(blocking_ids, reason, display_map)
                blocked_marker = f"[B:{b_labels}] "
        if project:
            print(f"#{label} {prompt_marker}{blocked_marker}| {status} | {priority} | {task_text}")
        else:
            out_marker = _checkout_marker(checkouts, task["project_id"])
            print(f"#{label} {prompt_marker}{blocked_marker}{out_marker}| {task['project_id']} | {status} | {priority} | {task_text}")
    status_counts = {}
    for task in task_list:
        status_counts[task["status"]] = status_counts.get(task["status"], 0) + 1
    summary_parts = [f"{s}: {status_counts[s]}" for s in ["Backlog", "To Do", "In Progress", "Review", "Done", "Cancelled"] if s in status_counts]
    print(f"\nTotal: {len(task_list)} tasks ({', '.join(summary_parts)})")


def _resolve_project_id(db, project):
    """Resolve a project id or name (case-insensitive) to its id, or None.

    An id match wins over a name match, so a project named after another
    project's id can't capture it. add_project keeps names unique, but a
    concurrent add can still leave two; a shared name is refused rather than
    picked by row order.
    """
    if not project: return None
    wanted = project.lower()
    projects = db.get_all_projects()
    for field in ("id", "name"):
        matches = [p["id"] for p in projects if p[field].lower() == wanted]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise click.UsageError(
                f"Project {field} '{project}' matches {len(matches)} projects "
                f"({', '.join(matches)}); use the exact id"
            )
    return None


def _resolve_task_id(db, token: int) -> int:
    """Resolve a display_id or Snowflake task_id to the canonical task PK.

    Returns the resolved task_id, or token unchanged if resolution fails
    (callers that pass a bad ID will get a "not found" from get_task anyway).
    """
    resolved = db.resolve_task_id(token)
    return resolved if resolved is not None else token


def _parse_blocked_by(db, blocked_by: str) -> str:
    """Turn a `--blocked-by` token list into a JSON array of canonical task PKs.

    Two things happen here, and both are load-bearing (#6747):

    1. Every token goes through ``db.resolve_task_id`` so display IDs work the
       way they do in every other command. `--blocked-by 6742` used to store
       the display ID verbatim, which matches no row.
    2. A token that resolves to nothing is refused outright. Storing it makes
       ``get_blocking_tasks`` drop it silently, ``is_blocked`` return
       ``(False, [])``, and the board report the card as unblocked while it is
       genuinely blocked — the exact opposite of the truth.

    Raises ``click.ClickException`` on anything unparseable or unresolvable.
    """
    import json as _json

    tokens = [t.strip() for t in blocked_by.split(",") if t.strip()]
    if not tokens:
        raise click.ClickException(
            "--blocked-by must be comma-separated task IDs (e.g., '4645,4646')"
        )

    resolved: List[int] = []
    unknown: List[str] = []
    for token in tokens:
        try:
            value = int(token)
        except ValueError:
            raise click.ClickException(
                "--blocked-by must be comma-separated task IDs "
                f"(e.g., '4645,4646'); got {token!r}"
            )
        task_pk = db.resolve_task_id(value)
        if task_pk is None:
            unknown.append(token)
        else:
            resolved.append(task_pk)

    if unknown:
        plural = "IDs" if len(unknown) > 1 else "ID"
        raise click.ClickException(
            f"--blocked-by names {plural} that match no task: "
            f"{', '.join('#' + u for u in unknown)}. Storing an unresolvable ID "
            f"would make this card report itself unblocked while it is blocked."
        )

    return _json.dumps(resolved)


def _blocked_by_report(db, task) -> Optional[str]:
    """One "Blocked by:" line for a task, or None when it has no blockers.

    Three outcomes are kept distinct so this can never again print the exact
    opposite of the truth (#6747). Before the write-time resolution fix, an
    unresolvable stored ID was silently dropped by ``get_blocking_tasks``,
    ``is_blocked`` answered ``(False, [])``, and this line read
    "(all resolved)" for a card that was genuinely blocked.
    """
    import json as _json

    raw = task.get("blocked_by")
    if not raw:
        return None
    try:
        stored = _json.loads(raw)
    except (TypeError, ValueError):
        return f"Blocked by: (malformed blocked_by value: {raw!r})"
    if not isinstance(stored, list):
        return f"Blocked by: (malformed blocked_by value: {raw!r})"
    if not stored:
        return None

    incomplete: List = []
    unknown: List = []
    for tid in stored:
        blocker = db.get_task(tid) if isinstance(tid, int) else None
        if blocker is None:
            unknown.append(tid)
        elif blocker["status"] != "Done":
            incomplete.append(tid)

    parts = []
    if incomplete:
        parts.append(f"{', '.join(f'#{t}' for t in incomplete)} (incomplete)")
    if unknown:
        parts.append(
            f"{', '.join(f'#{t}' for t in unknown)} (UNRESOLVED - no such task; "
            f"this card's blocked state cannot be trusted, re-set it with "
            f"`pt tasks update <id> --blocked-by ...`)"
        )
    if not parts:
        return "Blocked by: (all resolved)"
    return "Blocked by: " + "; ".join(parts)


def _detect_project_from_cwd(db):
    import os
    cwd = os.environ.get("PT_CALLER_CWD", os.getcwd())
    dir_name = os.path.basename(cwd)
    try:
        return _resolve_project_id(db, dir_name)
    except click.UsageError as e:
        # The caller typed no project; say where the name came from and how to pick.
        raise click.UsageError(
            f"Can't tell the project from directory '{dir_name}': {e.message}. Pass -p <id>."
        ) from e


def _run_git(repo_dir: Path, args: list[str], timeout: int = 10) -> tuple[str, int]:
    """Run a git command in repo_dir. Returns (stdout, returncode)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout, result.returncode


class PtJsonError(Exception):
    """Structured CLI error for JSON automation surfaces."""

    def __init__(self, error_class: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.exit_code = exit_code


def _json_dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _emit_json(payload: dict) -> None:
    click.echo(_json_dumps(payload))


def _emit_json_error(error: PtJsonError, command: str, *, err: bool = False) -> NoReturn:
    """Emit a structured JSON error and exit. Never returns.

    `err=True` routes the JSON to stderr instead of stdout — required for
    NDJSON streams (e.g. `memory export`) so a mid-stream error doesn't
    pollute the data channel.
    """
    payload = {
        "schema_version": PT_JSON_SCHEMA_VERSION,
        "ok": False,
        "command": command,
        "error": {
            "class": error.error_class,
            "message": error.message,
        },
    }
    click.echo(_json_dumps(payload), err=err)
    raise SystemExit(error.exit_code)


def _caller_repo_root() -> Path:
    """The repo root of the directory the operator actually ran `pt` from.

    `Path.cwd()` is useless here: the `pt` launcher does `cd "$launcher_dir"`
    before exec'ing, so inside pt.py the cwd is ALWAYS project-tracker. That is
    why `pt migration start` recorded the wrong repo for every project (#6851).
    The launcher exports the real directory as PT_CALLER_CWD, which the rest of
    the codebase already reads (_detect_project_from_cwd, scripts/origin.py);
    migration never got the memo.

    PT_CALLER_CWD is the load-bearing part. `git rev-parse --show-toplevel`
    on top of it additionally handles being invoked from a subdirectory of a
    repo, which `Path.cwd()` never did. Falls back to the caller cwd itself
    when that is not a git repo.

    `_find_repo_root()` used to carry this same bug by design — it tried
    `Path(__file__).parent.parent` first — and now delegates here (#6883), so
    this is the single place the caller's repo is resolved.
    """
    caller = Path(os.environ.get("PT_CALLER_CWD") or os.getcwd())
    try:
        stdout, rc = _run_git(caller, ["rev-parse", "--show-toplevel"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return caller
    if rc != 0:
        return caller
    top = stdout.strip()
    return Path(top) if top else caller
