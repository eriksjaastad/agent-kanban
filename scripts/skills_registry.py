"""Skills registry — enumerate installed Claude Code skills.

Walks a skills root directory (default ``~/.claude/skills/``) for every
``SKILL.md`` file at any depth, parses the YAML frontmatter, and returns
the canonical skill name — the string the ``Skill`` tool carries as
``tool_input.skill`` when a user invokes a slash command.

The canonical name is the frontmatter ``name:`` field, *not* the
directory name. Directory prefixes (``apple-``, ``blender-`` etc.) are
organisational; they are stripped in the name the agent actually sees.

Stdlib-only to stay importable from any hook/script.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---", re.DOTALL)
_NAME_LINE_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)

DEFAULT_SKILLS_ROOT = Path.home() / ".claude" / "skills"


def _warn_skipped(skill_md_path: Path, reason: str) -> None:
    """Emit a single-line stderr warning so operators see a dropped SKILL.md.

    Silent drops previously made ``--never-used`` report a skill as unused
    when the real problem was a parse failure on its SKILL.md.
    """
    print(
        f"skills_registry: skipping {skill_md_path} ({reason})",
        file=sys.stderr,
    )


def _extract_name(skill_md_path: Path) -> str | None:
    """Return the frontmatter ``name:`` value from a SKILL.md, or None.

    Emits a stderr warning on each skip reason (OSError, missing
    frontmatter block, missing ``name:`` field) so a malformed file
    surfaces instead of silently disappearing from the registry.
    """
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # governance: allow-silent SF002: None means "skip this SKILL.md"; the documented stderr skip warning names the file and read error
        _warn_skipped(skill_md_path, f"read error: {exc}")
        return None
    block = _FRONTMATTER_RE.match(text)
    if block is None:
        _warn_skipped(skill_md_path, "no YAML frontmatter block")
        return None
    name_match = _NAME_LINE_RE.search(block.group(1))
    if name_match is None:
        _warn_skipped(skill_md_path, "frontmatter missing 'name:' field")
        return None
    raw = name_match.group(1).strip().strip('"').strip("'")
    if not raw:
        _warn_skipped(skill_md_path, "empty 'name:' value")
        return None
    return raw


def installed_skills(root: Path | None = None) -> dict[str, Path]:
    """Return ``{canonical_name: SKILL.md path}`` for every installed skill.

    If two SKILL.md files declare the same canonical name the first one
    encountered wins and later duplicates are silently dropped (the Skill
    tool would have the same ambiguity to resolve at invocation time;
    de-duping here keeps the registry single-valued for reporting).

    ``root`` defaults to ``~/.claude/skills/`` resolved *at call time* so
    tests can monkeypatch ``$HOME`` and pick up a fixture directory.
    """
    if root is None:
        root = Path.home() / ".claude" / "skills"
    out: dict[str, Path] = {}
    if not root.is_dir():
        return out
    for md in root.rglob("SKILL.md"):
        name = _extract_name(md)
        if name and name not in out:
            out[name] = md
    return out
