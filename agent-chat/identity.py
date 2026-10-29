"""Agent Chat identity resolution.

An ADDRESS is a project name (ruling of 2026-08-30), what you type in
`--to`. It is stable and human-legible, and it survives session death.

An agent's own address comes from the project it was started in, never from
wherever it has `cd`'d since: a project-tracker session that reads
~/projects/notes must not start speaking as notes. The hook reads
CLAUDE_PROJECT_DIR, which Claude Code fixes for the session; `pt` reads the
directory it was called from, which Claude Code resets to the session's
project before every Bash call. Until #8734 (2026-10-27) the answer was frozen
into a file per session instead, about 900 of them.

The address is derived from the project NAME, never the path. Layouts diverge
between machines (one keeps a project under an underscore-prefixed name or at
a different depth than the other), so a path-derived identity works on one
machine and breaks on the next.

Machine qualifier is email-style and optional: `notes@mini` addresses one
specific agent when two machines work the same project. A bare `notes`
reaches whoever is on it. Qualify only when it matters.

Agent qualifier (#7146) disambiguates concurrent agents in the SAME directory:
`notes+codex` vs bare `notes`. Set `AGENT_CHAT_AGENT` (a short slug:
letters, digits, `.`, `_`, `-`) at session launch. Legacy sessions that omit it
keep the bare project address, so the existing corpus still delivers. When two
agents launch at `~/projects` without `AGENT_CHAT_AGENT`, both still resolve to
`claude-architect` — that collision is the bug this env var exists to prevent;
set e.g. `AGENT_CHAT_AGENT=codex` / `claude` so addresses become
`claude-architect+codex` and `claude-architect+claude`.
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path

# Markers that identify a project root, in priority order. .git first because
# it is the strongest signal; CLAUDE.md covers project dirs that are not
# repositories.
PROJECT_ROOT_MARKERS = (".git", "CLAUDE.md")

# The ~/projects root is not a project — it is the Architect's own directory,
# and the legacy corpus already uses this address for it.
ARCHITECT_ADDRESS = "claude-architect"


def human_address() -> str | None:
    """The person's own address, reserved so that no agent resolves to it; None when unset or empty.

    AGENT_CHAT_HUMAN, from the environment only: set it in the shell's
    environment (e.g. ~/.zshenv), which pt and the chat hook both inherit.
    The value must be a bare name (letters, digits, ".", "_", "-"), so no
    @machine or +agent part.
    """
    # governance: allow-silent SF003: AGENT_CHAT_HUMAN is optional; unset or empty means no address is reserved
    value = os.environ.get("AGENT_CHAT_HUMAN", "").strip()
    if not _HUMAN_NAME.fullmatch(value):
        # The value is not echoed: an environment can hold anything.
        raise ValueError("AGENT_CHAT_HUMAN must be a bare name: letters, digits, '.', '_', '-'")
    return value or None


# A human's address: the characters an address may hold, and nothing a shell could split.
_HUMAN_NAME = re.compile(r"[A-Za-z0-9._-]*")


# Addresses are used in URLs and jq comparisons; keep them boring.
# Local part may include a single `+agent` qualifier (#7146).
#
# A single leading underscore is allowed (#7798): an underscore-prefixed
# directory can be a real project, and the server already accepts such an
# address; only this grammar rejected it. The optional `_?` is followed by a
# mandatory alnum-anchored body, so an all-underscore name (`___`) still has
# no alnum character and still fails to match; a leading dot (`.claude`) is
# still outside the allowed lead-character set and still fails too. Both of
# those stay invalid on purpose — only the underscore case changed.
_VALID_ADDRESS = re.compile(r"^_?[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_VALID_AGENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def projects_root() -> Path:
    """Root directory containing all projects."""
    return Path(os.environ.get("PROJECTS_ROOT", str(Path.home() / "projects")))


def machine_name() -> str:
    """Short name for this machine, used as the optional @qualifier.

    Derived from the hostname rather than configured, so a new machine works
    without setup. `Office-Mac-mini` -> `mini`.
    """
    override = os.environ.get("AGENT_CHAT_MACHINE")
    if override:
        return override.strip().lower()

    host = socket.gethostname().split(".")[0].lower()
    if "mini" in host:
        return "mini"
    if "macbook" in host or "laptop" in host:
        return "laptop"
    # Unknown machine: use the hostname itself rather than guessing, so the
    # qualifier is still unique and debuggable.
    return re.sub(r"[^a-z0-9-]", "-", host) or "unknown"


def agent_name() -> str | None:
    """Optional agent slug from AGENT_CHAT_AGENT (or AGENT_CHAT_AGENT_NAME).

    Empty/unset means legacy bare project addressing. Invalid values are ignored
    rather than poisoning the address.
    """
    # governance: allow-silent SF003: the agent slug is optional; empty means legacy bare project addressing and returns None below
    raw = (
        os.environ.get("AGENT_CHAT_AGENT")
        or os.environ.get("AGENT_CHAT_AGENT_NAME")
        or ""
    ).strip().lower()
    if not raw:
        return None
    if not _VALID_AGENT.match(raw):
        return None
    if raw == (human_address() or "").lower():
        return None
    return raw


def with_agent(project: str, agent: str | None = None) -> str:
    """Attach an agent qualifier: `notes` + `codex` -> `notes+codex`."""
    if not agent:
        return project
    return f"{project}+{agent}"


def split_local(local: str) -> tuple[str, str | None]:
    """Split `notes+codex` into ("notes", "codex")."""
    if "+" in local:
        project, _, agent = local.partition("+")
        return project, (agent or None)
    return local, None


def find_project_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a project root marker.

    Returns None when `start` is not inside a project — the caller decides
    what that means rather than getting a silently wrong answer. A `start`
    that cannot be resolved raises: that is a failure, not "outside a project".
    """
    current = Path(start).resolve()

    root = projects_root()
    try:
        root = root.resolve()
    except (OSError, RuntimeError):  # governance: allow-silent SF001: the unresolved projects root is still the comparison target, the same fallback project_name_for applies
        pass

    for candidate in (current, *current.parents):
        # The projects root itself is the Architect's directory, not a project.
        if candidate == root:
            return candidate
        for marker in PROJECT_ROOT_MARKERS:
            if (candidate / marker).exists():
                return candidate
        # Never walk above the projects root.
        if candidate == candidate.parent:
            break
    return None


def project_name_for(cwd: str | Path) -> str | None:
    """Resolve the project NAME for a directory, or None if outside a project.

    Only the basename of the project root is used. The path that led there is
    deliberately discarded — that is what keeps this stable across machines
    with different layouts.
    """
    project_root = find_project_root(Path(cwd))
    if project_root is None:
        return None

    try:
        root = projects_root().resolve()
    except (OSError, RuntimeError):
        root = projects_root()

    if project_root == root:
        name = ARCHITECT_ADDRESS
    else:
        name = project_root.name
        if not name or not _VALID_ADDRESS.match(name):
            return None
    if name == human_address():
        # Reserved, whatever the address: an agent must never speak as the human.
        return None
    return name


def qualify(address: str, machine: str | None = None) -> str:
    """Attach a machine qualifier: `notes` + `mini` -> `notes@mini`."""
    if not machine:
        return address
    return f"{address}@{machine}"


def split_address(address: str) -> tuple[str, str | None]:
    """Split `notes+codex@mini` into ("notes+codex", "mini").

    The local part may itself contain a `+agent` qualifier; use `split_local`
    to peel that off. A bare `notes` returns ("notes", None), meaning
    "whoever bound that bare project address", which is the default and the
    common case.
    """
    if "@" in address:
        local, _, machine = address.partition("@")
        return local, (machine or None)
    return address, None


def address_for(directory: str | Path) -> str | None:
    """This agent's address when started in directory: its project, plus AGENT_CHAT_AGENT.

    None when directory is not inside a project.
    """
    name = project_name_for(directory)
    return with_agent(name, agent_name()) if name else None


def configured_sender() -> str | None:
    """AGENT_CHAT_SENDER, the fallback address outside any project.

    The human's reservation holds here too: AGENT_CHAT_SENDER set to the
    human's address must not be a way for an agent to speak as the human.
    """
    # governance: allow-silent SF003: AGENT_CHAT_SENDER is optional; empty returns None just below
    fallback = os.environ.get("AGENT_CHAT_SENDER", "").strip()
    if not fallback or is_human(fallback):
        return None
    return fallback


def is_human(address: str) -> bool:
    """True when address, bare or with @machine or +agent, is the reserved human's.

    Every sender an agent could use goes through this, so none may be the human.
    """
    human = human_address()
    project, _agent = split_local(split_address(address.strip())[0])
    return bool(human) and project == human
