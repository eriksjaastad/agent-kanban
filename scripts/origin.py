"""Origin resolution — which agent/context filed this card.

Used to populate the ``created_by`` field on tasks. Derives a short label
from a caller's cwd so the CLI, MCP server, and any future surface all
tag provenance consistently.

Two entry points:

* :func:`project_from_cwd` — pure function. Takes a cwd string, returns
  the label. Used by any code that already knows which cwd to resolve
  (e.g. rows from the memory store's ``skill_invocations`` table).
* :func:`resolve_created_by` — convenience wrapper that reads the caller's
  own cwd (honouring ``PT_CALLER_CWD``) and delegates to
  :func:`project_from_cwd`.
"""

from __future__ import annotations

import os
from pathlib import Path


def project_from_cwd(cwd: str | os.PathLike[str]) -> str:
    """Map a cwd path to a short project label.

    - ``~/projects`` exactly → ``"architect"``
    - inside ``~/projects/<name>/...`` → ``"<name>"``
    - empty string, missing path, or anywhere else → ``"unknown"``
    """
    if not cwd:
        return "unknown"
    try:
        resolved = Path(cwd).resolve()
        projects_root = (Path.home() / "projects").resolve()
    except Exception:
        return "unknown"
    if resolved == projects_root:
        return "architect"
    try:
        rel = resolved.relative_to(projects_root)
    except ValueError:
        return "unknown"
    return rel.parts[0] if rel.parts else "architect"


def resolve_created_by() -> str:
    """Infer a card's origin label from the caller's cwd.

    Honours ``PT_CALLER_CWD`` so wrappers can forward the user's cwd.
    """
    return project_from_cwd(os.environ.get("PT_CALLER_CWD", os.getcwd()))
