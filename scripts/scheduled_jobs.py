"""Everything scheduled to run on this Mac (#8377).

Why: the owner wants one place that answers "what runs on this Mac?", including jobs
that are installed but switched off. ``alert_digest.py`` shares the
``launchctl list`` parser below instead of keeping its own copy, so this module
imports no project modules: it loads the same way as ``scripts.scheduled_jobs``
and as a sibling of the digest script. (``mini_scan.py`` keeps its own copy
because it is piped over ssh as a standalone script.)
"""

import json
import plistlib
import re
import subprocess
from pathlib import Path

# Each external read (launchctl, crontab) gets this long before it is reported
# as failed.
SOURCE_TIMEOUT_S = 15


def parse_launchctl_list(stdout: str) -> list[dict]:
    """Rows of `launchctl list`: columns are PID, last-exit-status, Label.

    A numeric PID means currently running; the status is that of the previous
    run, and "-" means none. A malformed status becomes None, so one odd line
    cannot hide every other job.
    """
    rows = []
    for line in stdout.split("\n")[1:]:  # skip header; a label may hold any other line break
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid_s, status_s, label = parts
        try:
            last_exit = None if status_s == "-" else int(status_s)
        except ValueError:
            last_exit = None
        rows.append({"label": label, "pid": None if pid_s == "-" else pid_s, "last_exit": last_exit})
    return rows


def launchctl_list() -> list[dict]:
    """Run `launchctl list`. Raises on a nonzero exit or timeout; the caller decides."""
    # Bytes, decoded below: text mode would turn a "\r" inside a label into a line break.
    proc = subprocess.run(["launchctl", "list"], capture_output=True, timeout=SOURCE_TIMEOUT_S, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"launchctl exit {proc.returncode}: {proc.stderr.decode(errors='replace')[:200]}")
    return parse_launchctl_list(proc.stdout.decode(errors="replace"))


def describe_settings(plist: dict) -> str:
    """Every key in the plist except ``Label``, as compact JSON in file order.

    launchd has many keys that start or restart a job (calendar, interval,
    watched paths, KeepAlive, OnDemand, EnablePressuredExit, ...), so nothing
    is picked out: what is not shown cannot be missed. The one exception is
    ``EnvironmentVariables``, whose values can hold secrets: only its names are
    shown.
    """
    settings = {key: value for key, value in plist.items() if key != "Label"}
    if "EnvironmentVariables" in settings:
        env = settings["EnvironmentVariables"]
        settings["EnvironmentVariables"] = sorted(env) if isinstance(env, dict) else "<not a dictionary>"
    return json.dumps(settings, default=str, separators=(",", ":"))


WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # launchd and cron: 0 and 7 are Sunday
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
CALENDAR_RANGES = {"Minute": (0, 59), "Hour": (0, 23), "Day": (1, 31), "Weekday": (0, 7), "Month": (1, 12)}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _every(seconds: int) -> str:
    for size, unit in ((3600, "h"), (60, "min")):
        if seconds % size == 0:
            return f"every {seconds // size} {unit}"
    return f"every {seconds} s"


def describe_calendar(entry) -> str | None:
    """One launchd calendar entry in words ("daily 07:00", "Mon 09:30"), or None if it is not one.

    A missing key matches every value, as in launchd.plist(5) and cron.
    """
    if not isinstance(entry, dict) or not set(entry) <= set(CALENDAR_RANGES):
        return None
    if not all(_is_int(v) and CALENDAR_RANGES[k][0] <= v <= CALENDAR_RANGES[k][1] for k, v in entry.items()):
        return None
    minute, hour, day, weekday, month = (entry.get(k) for k in ("Minute", "Hour", "Day", "Weekday", "Month"))
    if hour is not None and minute is not None:
        time = f"{hour:02}:{minute:02}"
    elif minute is not None:
        time = f"at :{minute:02} every hour"
    elif hour is not None:
        time = f"every minute from {hour:02}:00 to {hour:02}:59"
    else:
        time = "every minute"
    days = " or ".join(x for x in (day and f"day {day}", weekday is not None and WEEKDAYS[weekday]) if x)
    when = " ".join(x for x in (month and MONTHS[month], days) if x)
    if not when and hour is not None and minute is not None:
        when = "daily"
    return f"{when} {time}".strip()


def describe_schedule(plist: dict) -> str:
    """The start triggers it recognises, in words; a value it cannot put in words is shown as it is.

    A summary only: launchd has more triggers than these, so the full settings
    stay on the row (``describe_settings``), and a plist with no recognised
    trigger says so instead of guessing.
    """
    parts = []
    if "StartInterval" in plist:
        interval = plist["StartInterval"]
        ok = _is_int(interval) and interval > 0
        parts.append(_every(interval) if ok else f"StartInterval {json.dumps(interval, default=str)}")
    if "StartCalendarInterval" in plist:
        calendar = plist["StartCalendarInterval"]
        entries = calendar if isinstance(calendar, list) else [calendar]
        words = [describe_calendar(entry) for entry in entries]
        ok = bool(words) and all(words)
        parts.append("; ".join(words) if ok else f"StartCalendarInterval {json.dumps(calendar, default=str)}")
    if plist.get("RunAtLoad") is True:
        parts.append("at login or load")  # RunAtLoad: whenever launchd loads it
    # launchd.plist(5): OnDemand false is the old spelling of KeepAlive true.
    keep_alive = plist.get("KeepAlive")
    if keep_alive is True or (keep_alive is None and plist.get("OnDemand") is False):
        parts.append("kept running")
    elif keep_alive not in (None, False):  # conditions: their values decide, so show them
        parts.append(f"KeepAlive {json.dumps(keep_alive, default=str, sort_keys=True)}")
    for key, words in (("WatchPaths", "when a watched path changes"), ("QueueDirectories", "when a queue folder fills"),
                       ("StartOnMount", "when a disk is mounted"), ("LaunchEvents", "on a system event"),
                       ("Sockets", "when another program calls it"), ("MachServices", "when another program calls it")):
        if plist.get(key) and words not in parts:
            parts.append(words)
    return ", ".join(parts) or "no schedule recognised; see Full settings"


CRON_NICKNAMES = {"@reboot": "at startup", "@yearly": "Jan day 1 00:00", "@annually": "Jan day 1 00:00",
                  "@monthly": "day 1 00:00", "@weekly": "Sun 00:00", "@daily": "daily 00:00",
                  "@midnight": "daily 00:00", "@hourly": "at :00 every hour"}


def describe_cron(schedule: str) -> str:
    """A crontab schedule in the same words as launchd's, or "cron <fields>" when it is not a plain one."""
    if schedule in CRON_NICKNAMES:
        return CRON_NICKNAMES[schedule]
    fields = schedule.split()
    if len(fields) == 5:
        # */N restarts at :00 each hour, so it is a steady interval only when N divides 60.
        step = re.fullmatch(r"\*/([1-9][0-9]*)", fields[0])
        if step and 60 % int(step.group(1)) == 0 and fields[1:] == ["*"] * 4:
            return _every(int(step.group(1)) * 60)
        if all(f == "*" or re.fullmatch(r"[0-9]+", f) for f in fields):
            keys = ("Minute", "Hour", "Day", "Month", "Weekday")
            if words := describe_calendar({k: int(f) for k, f in zip(keys, fields) if f != "*"}):
                return words
    return f"cron {schedule}"


# Project directories are lowercase letters, digits and dashes (owner
# decision, 2026-10-24), so a name can never contain a quote, a space or a shell operator.
PROJECT_NAME = r"[a-z0-9][a-z0-9-]*"


def project_of(texts, root: Path) -> str:
    """Best-effort owner: the first ``<root>/<project name>`` that ``texts`` mention.

    The root may also be spelled ``~``, ``$HOME`` or ``${HOME}`` plus its path
    under the home directory, as in a ``sh -c`` script. The match must begin a
    path (``/x/root/alpha`` is not under ``/root``) and the name must end where
    the path segment ends, so ``root/alpha_x`` and ``root/"alpha beta"`` name
    nothing.
    """
    spellings = [str(root)]
    if root.is_relative_to(Path.home()):
        rel = root.relative_to(Path.home())
        spellings += [f"{home}/{rel}" for home in ("~", "$HOME", "${HOME}")]
    roots = "|".join(re.escape(s) for s in spellings)
    pattern = re.compile(rf"(?<![\w./$~-])(?:{roots})/({PROJECT_NAME})(?![\w.-])")
    for text in texts:
        if isinstance(text, str) and (match := pattern.search(text)):
            return match.group(1)
    return ""


def _plist_row(path: Path, root: Path) -> dict:
    """A row for one plist. An unreadable or invalid one keeps its row, with the error.

    launchd requires a ``Label`` (launchd.plist(5)), so a plist that parses but
    has none is ``status`` "invalid": launchd cannot load it, so it is no job's
    installed copy. Its file name is only for display and is never matched.
    """
    row = {"source": "launchd", "label": path.stem, "project": "", "settings": "", "schedule": "",
           "path": str(path), "error": None, "status": None, "running": False}
    try:
        with path.open("rb") as f:
            plist = plistlib.load(f)
        if not isinstance(plist, dict):
            raise TypeError("top level is not a dictionary")
        row["settings"] = describe_settings(plist)
        label = plist.get("Label")
        if not isinstance(label, str) or not label.strip():
            row.update(status="invalid", error=f"invalid plist: Label is {label!r}; launchd cannot load it")
            return row
        row["label"] = label
        row["schedule"] = describe_schedule(plist)
        args = plist.get("ProgramArguments")
        texts = (args if isinstance(args, list) else []) + [plist.get("Program"), plist.get("WorkingDirectory")]
        row["project"] = project_of(texts, root)
    except Exception as exc:  # noqa: BLE001 - plistlib raises several types; each one is shown on the row
        row["error"] = f"unreadable plist: {exc}"
    return row


def _listdir(directory: Path, errors: list[str], missing_ok: bool) -> list[Path] | None:
    """Sorted entries of ``directory``, or None if it could not be read (added to ``errors``).

    With ``missing_ok``, a path that does not exist or is not a directory is
    expected absence and gives []; otherwise it is a read failure too.
    """
    try:
        return sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError) as exc:  # governance: allow-silent SF002: absence the caller expects is truly no entries; otherwise reported
        if missing_ok:
            return []
        errors.append(f"cannot read {directory}: {exc.strerror}")
    except OSError as exc:
        errors.append(f"cannot read {directory}: {exc}")
    return None


def launchd_jobs(home: Path, root: Path, errors: list[str]) -> list[dict]:
    """Installed LaunchAgents with their status, then repo plists that are not installed.

    Status: active (loaded), inactive (installed, not loaded), not installed
    (only in a repo's launchd/ dir), invalid (no Label), or unknown. Only a
    plist that was read and has a Label has a known identity, so only it is
    matched against launchctl and the other directory. Anything that could
    not be read makes the claims it would decide unknown: launchctl failing makes every installed job unknown, and an
    unreadable LaunchAgents directory or plist means no repo job can be called
    not installed.
    """
    try:
        loaded = {r["label"]: r for r in launchctl_list()}
    except Exception as exc:  # noqa: BLE001 - reported to the caller in `errors`
        loaded = None
        errors.append(f"launchctl list failed: {exc}")

    repo_rows = []
    for project in _listdir(root, errors, missing_ok=False) or []:
        for path in _listdir(project / "launchd", errors, missing_ok=True) or []:
            if path.suffix == ".plist":
                repo_rows.append({**_plist_row(path, root), "project": project.name})
    repo_project: dict[str, str] = {}  # an installed copy's owner; the first repo (sorted) wins a shared label
    for row in repo_rows:
        if not row["error"]:
            repo_project.setdefault(row["label"], row["project"])

    agents = _listdir(home / "Library" / "LaunchAgents", errors, missing_ok=True)
    jobs = [_plist_row(path, root) for path in agents or [] if path.suffix == ".plist"]
    for row in jobs:
        state = None
        if not row["error"]:
            row["project"] = row["project"] or repo_project.get(row["label"], "")
        if row["status"] == "invalid":
            pass
        elif row["error"] or loaded is None:
            row["status"] = "unknown"
        else:
            state = loaded.get(row["label"])
            row["status"] = "active" if state else "inactive"
        row["last_exit"] = state["last_exit"] if state else None
        row["running"] = bool(state and state["pid"])

    # An invalid plist was read and is no job, so only an unreadable one hides what is installed.
    inventory_known = agents is not None and not any(row["error"] and row["status"] != "invalid" for row in jobs)
    installed = {row["label"] for row in jobs if not row["error"]}
    for row in repo_rows:
        if not row["error"] and row["label"] in installed:
            continue  # shown once, as the installed job
        status = row["status"] or ("not installed" if inventory_known and not row["error"] else "unknown")
        jobs.append({**row, "status": status, "last_exit": None})
    return jobs


# crontab(5): a line is blank, a comment, an environment setting
# (``name = value``; the name may be quoted), or a job: five time fields and a
# command, or an @nickname and a command. `crontab` checks the fields when the
# table is installed, so only their characters are matched here. Like cron,
# settings are checked first: "@reboot =x" sets a variable named @reboot.
CRON_ENV = re.compile(r"""\s*(?:"[^"]*"|'[^']*'|[^\s=]+)\s*=""")
CRON_FIELD = r"[0-9A-Za-z*,/-]+"
CRON_JOB = re.compile(
    rf"\s*(@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)|(?:{CRON_FIELD}[ \t]+){{4}}{CRON_FIELD})"
    r"[ \t]+(\S.*)"
)


def parse_crontab(text: str, root: Path) -> list[dict]:
    """One row per job line of ``crontab -l``; a line that is no job, setting or comment is an error row.

    The status is "in crontab", not active: cron keeps no record of runs, and
    on this MacBook crontab jobs have not run at all (the Muffin Pan Recipes
    job's log stops in March 2026).
    """
    rows = []
    for line in text.split("\n"):  # cron splits on newline only, not splitlines()'s other breaks
        if not line.strip() or line.lstrip().startswith("#") or CRON_ENV.match(line):
            continue
        row = {"source": "cron", "label": line.strip(), "project": "", "settings": "", "path": "crontab",
               "error": None, "status": "in crontab", "last_exit": None, "schedule": "", "running": False}
        if match := CRON_JOB.fullmatch(line):
            schedule, command = match.groups()
            settings = " ".join(schedule.split())
            row.update(label=command.strip(), settings=settings, schedule=describe_cron(settings),
                       project=project_of([command], root))
        else:
            row.update(status="unknown", error="not a crontab job, setting or comment")
        rows.append(row)
    return rows


def crontab_jobs(root: Path, errors: list[str]) -> list[dict]:
    """Rows for this user's crontab. No crontab at all is expected and gives no rows."""
    try:
        # Bytes, decoded below: text mode would turn a "\r" inside a command into a line break.
        proc = subprocess.run(["crontab", "-l"], capture_output=True, timeout=SOURCE_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:  # governance: allow-silent SF002: the failure is added to `errors`, which the page shows
        errors.append(f"crontab -l failed: {exc}")
        return []
    stdout, stderr = (out.decode(errors="replace") for out in (proc.stdout, proc.stderr))
    if proc.returncode == 1 and "no crontab" in stderr.lower():
        return []
    if proc.returncode != 0:
        errors.append(f"crontab -l failed: exit {proc.returncode}: {stderr.strip()[:200]}")
        return []
    return parse_crontab(stdout, root)


def scheduled_jobs(home: Path, root: Path) -> dict:
    """``{"jobs": [...], "errors": [...]}``: launchd jobs, then crontab jobs.

    A source that could not be read is named in ``errors``, so an empty list
    never stands in for a failure.
    """
    errors: list[str] = []
    jobs = launchd_jobs(home, root, errors) + crontab_jobs(root, errors)
    return {"jobs": jobs, "errors": errors}
