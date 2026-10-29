"""Single-line checklist toggling for card notes (#7821).

The dashboard ticks a "- [ ] text" line through a server-side atomic
toggle. This module owns the pure text edit: change exactly one marker and
leave every other byte of the notes (indentation, CRLF, other lines)
alone. Line indexes are positions in ``notes.split("\\n")``; a trailing
"\\r" from CRLF notes is not part of the item text.
"""

from __future__ import annotations

import re

# prefix "- [", marker, "] " + item text. Mirrors the frontend parser in
# dashboard/frontend/src/utils/checklist.ts.
CHECKLIST_LINE_RE = re.compile(r'^([ \t]*-[ \t]*\[)([ xX])(\][ \t]+)(\S.*)$')


class ChecklistLineError(ValueError):
    """The requested line is missing, not a checklist line, or has changed."""


def toggle_checklist_line(
    notes: str | None, line_index: int, expected_text: str, checked: bool
) -> str:
    """Return ``notes`` with only the marker on ``line_index`` set."""
    lines = (notes or "").split("\n")
    if line_index < 0 or line_index >= len(lines):
        raise ChecklistLineError(f"Line {line_index} is out of range")
    line = lines[line_index]
    body, cr = (line[:-1], "\r") if line.endswith("\r") else (line, "")
    match = CHECKLIST_LINE_RE.match(body)
    if not match:
        raise ChecklistLineError(f"Line {line_index} is not a checklist item")
    if match.group(4) != expected_text:
        raise ChecklistLineError(f"Line {line_index} no longer matches the expected text")
    current = match.group(2)
    if (current != " ") == checked:
        # Already in the requested state: leave the marker byte-for-byte,
        # so "[X]" stays "[X]" and a no-op request writes nothing.
        return notes or ""
    marker = "x" if checked else " "
    lines[line_index] = f"{match.group(1)}{marker}{match.group(3)}{match.group(4)}{cr}"
    return "\n".join(lines)
