"""Configuration for project tracker.

SAFETY NOTES:
- Database location should ALWAYS be data/tracker.db (the canonical path)
- PT_DB_PATH override is allowed but triggers loud warnings
- See Task #4692 for the history of database location confusion
"""

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def projects_root() -> Path:
    """Return the portfolio projects root.

    Honours ``PROJECTS_ROOT`` when it is set and non-empty; otherwise falls
    back to ``~/projects``. This is deliberately env-first so a git worktree
    (whose ``Path(__file__)``-relative idioms resolve inside ``.claude/``)
    still finds the real portfolio root.
    """
    env_root = os.environ.get("PROJECTS_ROOT")
    if env_root and env_root.strip():
        return Path(env_root)
    return Path.home() / "projects"


# Base directory for projects (can be overridden by PROJECTS_ROOT env var)
PROJECTS_BASE_DIR = projects_root()

# Validate an explicit PROJECTS_ROOT override: pointing it at a missing
# directory is a configuration error. The default ~/projects may legitimately
# not exist yet (CI runners, fresh checkouts), so it is allowed to be absent.
# governance: allow-silent SF003: the override is optional; empty means the ~/projects default and only a non-empty override is validated here
if os.environ.get("PROJECTS_ROOT", "").strip() and not PROJECTS_BASE_DIR.exists():
    raise ValueError(
        f"PROJECTS_ROOT path does not exist: {PROJECTS_BASE_DIR}\n"
        f"Please set PROJECTS_ROOT environment variable to a valid directory path."
    )

# CANONICAL database location - this is the ONLY correct path
_CANONICAL_DB_PATH = PROJECT_ROOT / "data" / "tracker.db"

# Database location (can be overridden by PT_DB_PATH env var - but we warn loudly!)
_db_path_override = os.getenv("PT_DB_PATH")
if _db_path_override:
    DATABASE_PATH = Path(_db_path_override)
    # LOUD WARNING: Non-canonical database path
    if DATABASE_PATH.resolve() != _CANONICAL_DB_PATH.resolve():
        print(f"⚠️  WARNING: PT_DB_PATH is set to non-canonical location!", file=sys.stderr)
        print(f"   Current:   {DATABASE_PATH}", file=sys.stderr)
        print(f"   Canonical: {_CANONICAL_DB_PATH}", file=sys.stderr)
        print(f"   This can cause data to be written to the wrong database!", file=sys.stderr)
        print(f"   To fix: unset PT_DB_PATH or set it to the canonical path.", file=sys.stderr)
else:
    DATABASE_PATH = _CANONICAL_DB_PATH

# External backup directory - survives project directory accidents
# Can be overridden for sandboxed environments (like Codex)
_external_backup_env = os.getenv("PT_EXTERNAL_BACKUP_DIR")
if _external_backup_env:
    EXTERNAL_BACKUP_DIR = Path(_external_backup_env)
else:
    EXTERNAL_BACKUP_DIR = Path.home() / ".project-tracker" / "backups"

# External resources file (can be overridden by PT_RESOURCES_FILE env var)
EXTERNAL_RESOURCES_FILE = Path(
    os.getenv(
        "PT_RESOURCES_FILE",
        PROJECT_ROOT / "EXTERNAL_RESOURCES.yaml"
    )
)

# Ensure data directory exists
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)


# --- Local integrations ------------------------------------------------------
# What this install connects to beyond its own database lives in one private,
# machine-local TOML file, never in the code: the repository ships no
# knowledge of its owner's other projects. An integration with no section is
# off, and the commands that need it say so. A file that exists but can't be
# read or parsed raises, so a typo never turns an integration off silently.
#
#   [memory]                          # a brain.py + brain.db memory store
#   root = "~/path/to/memory-store"   # directory holding brain.py and brain.db;
#                                     #   a relative root is relative to this file
#   doppler_project = "memory"        # optional: run brain.py under doppler run
#   doppler_config = "dev"            #   --project/--config (both or neither)
#   build_receipt = "logs/build.json" # optional: the graph build's JSON receipt,
#                                     #   relative to root, for the dashboard
#   build_schedule = "Mondays 10:00"  # optional: when that build runs, as shown
#
#   [chat]                            # Agent Chat (pt message)
#   addresses = ["architect", "me"]   # names a message may go to besides the
#                                     #   tracked projects: agents and people; a bare
#                                     #   name reaches any machine, name@machine one
#
#   [digest]                          # the daily alert email (scripts/alert_digest.py)
#   to = "<recipient address>"        # recipient and sender, both required
#   from = "Alerts <sender address>"
#   job_label_prefixes = ["com.example."]  # optional: launchd jobs to report on
#   ignore = ["scratch"]              # optional: this machine's projects to leave out
#   mini_ignore = ["archive"]         # optional: the second Mac's projects to leave out
#   mini_host = "other-mac"           # optional: a second Mac, scanned over ssh
#   doppler_project = "mail"          # optional: run under doppler run for
#   doppler_config = "prd"            #   RESEND_API_KEY (both or neither)
#   [[digest.attention]]              # optional, repeatable: a job whose own docs
#   label = "com.example.monitor"     #   call this exit code "needs attention",
#   exit_code = 2                     #   not a failure; shown as ATTENTION with
#   report = "~/monitor/report.md"    #   the report to read instead of FAILED
#
#   [morning]                         # the dashboard's Morning page
#   readme = "~/plans/README.md"      # file with a "## Standing morning plan" section
#   link_base = "https://example.com/plans/blob/main"  # where the plan's relative
#                                     #   links point: the readme's directory
#   snapshots = "snapshots"           # optional: directory of the day's hn-jobs-*.txt
#                                     # readme and snapshots: ~ expanded, relative to
#                                     #   this file; read only by the page, which
#                                     #   reports a missing or unreadable file
#
#   [graph]                           # the project graph (scripts/discovery/graph_builder.py)
#   skip_dirs = ["scratch"]           # optional: directory names to skip, beside the
#                                     #   generic ones in config/scan_config.yaml
#   ignore_projects = ["writing"]     # optional: projects the graph leaves out entirely
#
#   [jobs]                            # the dashboard's Jobs page
#   tailor_rules = "tailor-rules.md"  # optional: house rules for the tailor-resume
#                                     #   prompt, placed like [morning] readme;
#                                     #   {company} becomes the job's company
#   fit_keywords = ["python"]         # optional: words that raise a listing's fit
#                                     #   score (see scripts/db/job_ranking.py); none
#                                     #   means no fit boost
#
#   [addons]                          # add-ons in addons/ are on unless listed here
#   disabled = ["reminders"]          # written by `pt addons enable/disable`;
#                                     #   their commands and pages are hidden,
#                                     #   their data stays


def local_config_path() -> Path:
    """The local config file: $PT_CONFIG, else ~/.config/project-tracker/config.toml.

    Only tests set PT_CONFIG, always to an absolute path, so that is the one form
    accepted: local_config() refuses any other. Never raises and reads nothing,
    so it is safe in messages and diagnostics. Every failure to load the config
    is a ValueError from local_config() or memory_integration() that names the file.
    """
    # governance: allow-silent SF003: PT_CONFIG is optional; empty means the default path
    override = os.environ.get("PT_CONFIG", "").strip()
    return Path(override) if override else Path.home() / ".config" / "project-tracker" / "config.toml"


def local_config() -> dict:
    """The parsed local config file, or {} when there is none."""
    path = local_config_path()
    if not path.is_absolute():
        raise ValueError(f"PT_CONFIG={str(path)!r} must be an absolute path")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:  # governance: allow-silent SF002: no config file is the documented "no integrations" state
        return {}
    except (OSError, ValueError) as exc:  # a directory, no permission, a NUL in the path
        raise ValueError(f"{path} can't be read: {exc}") from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc


@dataclass(frozen=True)
class MemoryIntegration:
    """Where the optional memory store lives and how its brain.py is run."""

    root: Path
    doppler_project: str | None = None
    doppler_config: str | None = None
    build_receipt: str | None = None
    build_schedule: str | None = None

    @property
    def brain_py(self) -> Path:
        return self.root / "brain.py"

    @property
    def brain_db(self) -> Path:
        return self.root / "brain.db"


MEMORY_NOT_CONFIGURED = (
    "the memory integration is not configured: add a [memory] section with root = "
    "\"<directory holding brain.py and brain.db>\" to {path}"
)


def memory_integration(data: dict | None = None) -> MemoryIntegration | None:
    """The [memory] section of the local config, or None when it is absent. Invalid values raise.

    data is an already-loaded local_config(), so a caller checking several
    sections reads the file once; None reads it.
    """
    path = local_config_path()
    section = (local_config() if data is None else data).get("memory")
    if section is None:
        return None
    allowed = {"root", "doppler_project", "doppler_config", "build_receipt", "build_schedule"}
    if not isinstance(section, dict) or not set(section) <= allowed:
        raise ValueError(f"{path}: [memory] may only set {', '.join(sorted(allowed))}")
    values = {key: section.get(key) for key in allowed}
    if not all(v is None or (isinstance(v, str) and v.strip()) for v in values.values()) or not values["root"]:
        raise ValueError(f"{path}: [memory] root is required, and every value must be a non-empty string")
    if (values["doppler_project"] is None) != (values["doppler_config"] is None):
        raise ValueError(f"{path}: [memory] doppler_project and doppler_config go together")
    try:
        root = Path(values["root"]).expanduser()
        if not root.is_absolute():
            root = path.parent / root
        root = root.resolve()
    except (OSError, RuntimeError, ValueError) as exc:  # an unknown ~user, a NUL, a symlink loop
        raise ValueError(f"{path}: [memory] root {values['root']!r} can't be resolved: {exc}") from exc
    return MemoryIntegration(root, values["doppler_project"], values["doppler_config"],
                             values["build_receipt"], values["build_schedule"])


def chat_addresses(data: dict | None = None) -> set[str]:
    """[chat] addresses from the local config (data, as for memory_integration), or an empty set.

    Invalid values raise.
    """
    path = local_config_path()
    section = (local_config() if data is None else data).get("chat", {})
    if not isinstance(section, dict) or not set(section) <= {"addresses"}:
        raise ValueError(f"{path}: [chat] may only set addresses")
    addresses = section.get("addresses", [])
    if not isinstance(addresses, list) or not all(isinstance(a, str) and a and a == a.strip() for a in addresses):
        raise ValueError(f"{path}: [chat] addresses must be a list of names without surrounding spaces")
    return set(addresses)


@dataclass(frozen=True)
class DigestIntegration:
    """Who the daily alert digest goes to, and what it watches besides this machine."""

    to: str
    sender: str
    job_label_prefixes: tuple[str, ...] = ()
    mini_host: str | None = None
    doppler_project: str | None = None
    doppler_config: str | None = None
    # (launchd label, exit code) -> the report that explains it
    attention: dict = field(default_factory=dict)
    # Projects left out of the email: this machine's, and the second Mac's
    ignore: frozenset[str] = frozenset()
    mini_ignore: frozenset[str] = frozenset()


DIGEST_NOT_CONFIGURED = (
    "the alert digest is not configured: add a [digest] section with to and from to {path}"
)


def digest_integration(data: dict | None = None) -> DigestIntegration | None:
    """The [digest] section (data, as for memory_integration), or None when it is absent. Invalid values raise."""
    path = local_config_path()
    section = (local_config() if data is None else data).get("digest")
    if section is None:
        return None
    allowed = {"to", "from", "job_label_prefixes", "mini_host", "doppler_project", "doppler_config", "attention",
               "ignore", "mini_ignore"}
    if not isinstance(section, dict) or not set(section) <= allowed:
        raise ValueError(f"{path}: [digest] may only set {', '.join(sorted(allowed))}")

    def name(value):
        return isinstance(value, str) and bool(value) and value == value.strip()

    prefixes, host = section.get("job_label_prefixes", []), section.get("mini_host")
    ignored = section.get("ignore", []), section.get("mini_ignore", [])
    project, doppler_config = section.get("doppler_project"), section.get("doppler_config")
    # A host starting with "-" would reach ssh as an option, not a host.
    if not (name(section.get("to")) and name(section.get("from")) and (host is None or name(host) and host[0] != "-")
            and all(isinstance(v, list) and all(map(name, v)) for v in (prefixes, *ignored))
            and all(v is None or name(v) for v in (project, doppler_config))):
        raise ValueError(f"{path}: [digest] to and from are required, and every value (each of "
                         "job_label_prefixes, ignore and mini_ignore) must be a non-empty string without surrounding "
                         "spaces, and mini_host not start with -")
    if (project is None) != (doppler_config is None):
        raise ValueError(f"{path}: [digest] doppler_project and doppler_config go together")
    entries = section.get("attention", [])
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and set(e) == {"label", "exit_code", "report"} and name(e["label"])
            and name(e["report"]) and type(e["exit_code"]) is int and 0 < e["exit_code"] < 256 for e in entries):
        raise ValueError(f"{path}: each [[digest.attention]] needs exactly label, exit_code (1-255) and report")
    attention = {(e["label"], e["exit_code"]): e["report"] for e in entries}
    return DigestIntegration(section["to"], section["from"], tuple(prefixes), host, project, doppler_config,
                             attention, frozenset(ignored[0]), frozenset(ignored[1]))


@dataclass(frozen=True)
class MorningIntegration:
    """Where the dashboard's Morning page reads its plan and the day's snapshots.

    The paths are strings, spelled as configured: pathlib would drop a trailing
    "/" or "/." that the OS treats as meaningful.
    """

    readme: str
    link_base: str
    snapshots: str | None = None


MORNING_NOT_CONFIGURED = (
    "the Morning page is not configured: add a [morning] section with readme and link_base to {path}"
)


def morning_integration(data: dict | None = None) -> MorningIntegration | None:
    """The [morning] section (data, as for memory_integration), or None when it is absent. Invalid values raise."""
    path = local_config_path()
    section = (local_config() if data is None else data).get("morning")
    if section is None:
        return None
    allowed = {"readme", "link_base", "snapshots"}
    if not isinstance(section, dict) or not set(section) <= allowed:
        raise ValueError(f"{path}: [morning] may only set {', '.join(sorted(allowed))}")
    if not all(isinstance(v, str) and v and v == v.strip() for v in section.values()) or not (
            "readme" in section and section.get("link_base", "").rstrip("/")):
        raise ValueError(f"{path}: [morning] readme and link_base are required, and every value "
                         "must be a non-empty string without surrounding spaces")

    return MorningIntegration(_place(path, "morning", "readme", section["readme"]), section["link_base"].rstrip("/"),
                              _place(path, "morning", "snapshots", section["snapshots"]) if "snapshots" in section
                              else None)


def _jobs_section(path: Path, data: dict | None) -> dict:
    section = (local_config() if data is None else data).get("jobs", {})
    if not isinstance(section, dict) or not set(section) <= {"tailor_rules", "fit_keywords"}:
        raise ValueError(f"{path}: [jobs] may only set fit_keywords, tailor_rules")
    return section


def tailor_rules(data: dict | None = None) -> str | None:
    """[jobs] tailor_rules (data, as for memory_integration), placed like [morning] readme, or None when unset.

    Invalid values raise.
    """
    path = local_config_path()
    section = _jobs_section(path, data)
    rules = section.get("tailor_rules")
    if rules is None:
        return None
    if not (isinstance(rules, str) and rules and rules == rules.strip()):
        raise ValueError(f"{path}: [jobs] tailor_rules must be a non-empty string without surrounding spaces")
    return _place(path, "jobs", "tailor_rules", rules)


def fit_keywords(data: dict | None = None) -> tuple[str, ...]:
    """[jobs] fit_keywords (data, as for memory_integration); empty when unset. Invalid values raise."""
    path = local_config_path()
    words = _jobs_section(path, data).get("fit_keywords", [])
    if not (isinstance(words, list) and all(isinstance(w, str) and w and w == w.strip() for w in words)):
        raise ValueError(f"{path}: [jobs] fit_keywords must be a list of words without surrounding spaces")
    return tuple(words)


def _place(path: Path, section: str, key: str, value: str) -> str:
    """A configured file path: ~ expanded and joined to the config's directory, otherwise as written.

    Nothing touches the filesystem, and the spelling is kept (pathlib would drop a
    trailing "/" the OS treats as meaningful): the reader opens it and reports
    whatever the OS says.
    """
    if "\0" in value:
        raise ValueError(f"{path}: [{section}] {key} contains a NUL")
    expanded = os.path.expanduser(value)
    if expanded.startswith("~"):  # expanduser leaves an unknown ~user as written
        raise ValueError(f"{path}: [{section}] {key} {value!r} can't be expanded: no such user")
    return os.path.join(str(path.parent), expanded)


def graph_settings(data: dict | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """[graph] skip_dirs and ignore_projects (data, as for memory_integration); empty when unset.

    Invalid values raise.
    """
    path = local_config_path()
    section = (local_config() if data is None else data).get("graph", {})
    allowed = {"skip_dirs", "ignore_projects"}
    if not isinstance(section, dict) or not set(section) <= allowed:
        raise ValueError(f"{path}: [graph] may only set {', '.join(sorted(allowed))}")
    lists = [section.get(key, []) for key in ("skip_dirs", "ignore_projects")]
    if not all(isinstance(names, list) and all(isinstance(n, str) and n and n == n.strip() for n in names)
               for names in lists):
        raise ValueError(f"{path}: [graph] skip_dirs and ignore_projects must be lists of names "
                         "without surrounding spaces")
    return frozenset(lists[0]), frozenset(lists[1])


def disabled_addons(data: dict | None = None) -> frozenset[str]:
    """[addons] disabled (data, as for memory_integration): add-ons that stay off.

    Empty when unset. Invalid values raise.
    """
    path = local_config_path()
    section = (local_config() if data is None else data).get("addons", {})
    if not isinstance(section, dict) or not set(section) <= {"disabled"}:
        raise ValueError(f"{path}: [addons] may only set disabled")
    names = section.get("disabled", [])
    if not (isinstance(names, list) and all(isinstance(n, str) and n and n == n.strip() for n in names)):
        raise ValueError(f"{path}: [addons] disabled must be a list of add-on names without surrounding spaces")
    return frozenset(names)


def user_facing_projects(data: dict | None = None) -> frozenset[str]:
    """[dashboard] user_facing (data, as for memory_integration): projects whose work reaches real users.

    The owner keeps this list by hand; the dashboard's weekly split reads it on every
    request, so editing the file is enough. Empty when unset. Invalid values raise.
    """
    path = local_config_path()
    section = (local_config() if data is None else data).get("dashboard", {})
    if not isinstance(section, dict) or not set(section) <= {"user_facing"}:
        raise ValueError(f"{path}: [dashboard] may only set user_facing")
    names = section.get("user_facing", [])
    if not (isinstance(names, list) and all(isinstance(n, str) and n and n == n.strip() for n in names)):
        raise ValueError(f"{path}: [dashboard] user_facing must be a list of project ids without surrounding spaces")
    return frozenset(names)


def owner_name(data: dict | None = None) -> str:
    """[owner] name (data, as for memory_integration): the person the board waits on.

    A card that needs them starts with owner_prefix(), their name in capitals and
    a colon. "Owner" when unset. Invalid values raise.
    """
    path = local_config_path()
    section = (local_config() if data is None else data).get("owner", {})
    if not isinstance(section, dict) or not set(section) <= {"name"}:
        raise ValueError(f"{path}: [owner] may only set name")
    name = section.get("name", "Owner")
    if not (isinstance(name, str) and name and name == name.strip() and ":" not in name):
        raise ValueError(f"{path}: [owner] name must be a non-empty name without surrounding spaces or a colon")
    return name


def owner_prefix(name: str) -> str:
    """The text a card waiting on the owner starts with: "OWNER:" for "Owner"."""
    return f"{name.upper()}:"
