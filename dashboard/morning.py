"""Morning warm-up checklist page data.

The step list lives in a README named by the local config's [morning] section
(see scripts/config.py) and is read from there at request time, never
hand-copied into code. Parsing produces inline-markdown
"segments" so the frontend never needs dangerouslySetInnerHTML.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote
from datetime import date
from pathlib import Path

from scripts.config import MORNING_NOT_CONFIGURED, local_config_path, morning_integration
from scripts.logger import get_logger


logger = get_logger(__name__)


class MorningPlanError(RuntimeError):
    """Raised when the morning plan source is missing or cannot be parsed."""


_SECTION_HEADING = "## Standing morning plan"
_NEXT_MORNING_HEADING = "### Next morning"
# The target allows one level of balanced parentheses, e.g. .../Foo_(bar).
_LINK_RE = re.compile(r"\[([^\]]*)\]\(((?:[^()\s]|\([^()\s]*\))*)\)")


def _link_href(target: str, base: str) -> str:
    """Resolve a link target: absolute URLs pass through, relative ones resolve against base.

    base is the readme's URL (link_base plus its file name), so a fragment
    points into the readme and a path sits beside it.
    """
    target = target.strip()
    if target.startswith("./"):
        target = target[2:]
    if target.startswith("#"):
        return f"{base}{target}"
    if re.match(r"^(https?://|mailto:)", target, flags=re.IGNORECASE):
        return target
    return f"{base.rsplit('/', 1)[0]}/{target}"


def _inline_segments(text: str, base: str) -> list[dict]:
    """Split one paragraph into inline segments: text, strong, code, link."""
    segments: list[dict] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "`":
            end = text.find("`", i + 1)
            if end == -1:
                segments.append({"type": "text", "text": text[i:]})
                break
            segments.append({"type": "code", "text": text[i + 1 : end]})
            i = end + 1
        elif text.startswith("**", i):
            end = text.find("**", i + 2)
            if end == -1:
                segments.append({"type": "text", "text": text[i:]})
                break
            segments.append({"type": "strong", "text": text[i + 2 : end]})
            i = end + 2
        elif char == "[":
            match = _LINK_RE.match(text, i)
            if match:
                label, target = match.group(1), match.group(2)
                # Flatten any inline markup inside the label into plain link text.
                flat_label = "".join(
                    part["text"] for part in _inline_segments(label, base)
                )
                segments.append({
                    "type": "link",
                    "text": flat_label,
                    "href": _link_href(target, base),
                })
                i = match.end()
            else:
                segments.append({"type": "text", "text": char})
                i += 1
        else:
            end = i
            while end < len(text):
                if text[end] in "`[" or text.startswith("**", end):
                    break
                end += 1
            segments.append({"type": "text", "text": text[i:end]})
            i = end

    merged: list[dict] = []
    for segment in segments:
        if (
            segment["type"] == "text"
            and merged
            and merged[-1]["type"] == "text"
        ):
            merged[-1]["text"] += segment["text"]
        else:
            merged.append(segment)
    return merged


def _paragraphs(lines: list[str], base: str) -> list[list[dict]]:
    """Group lines into paragraphs, joining wrapped lines with a space, then inline-parse each."""
    paragraphs: list[list[dict]] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(_inline_segments(" ".join(current), base))
                current = []
        else:
            current.append(stripped)
    if current:
        paragraphs.append(_inline_segments(" ".join(current), base))
    return paragraphs


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|")


def _table_cells(line: str) -> list[str]:
    """Split a markdown table row into trimmed cells (no leading/trailing pipes)."""
    parts = line.strip().split("|")
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [part.strip() for part in parts]


def _find_section_bounds(lines: list[str]) -> tuple[int, int]:
    """Return (start, end) line indices for the Standing morning plan section."""
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == _SECTION_HEADING:
            start = idx
            break
    if start is None:
        raise MorningPlanError(
            f"Could not find '{_SECTION_HEADING}' section in the morning plan source"
        )

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].strip().startswith("## "):
            end = idx
            break
    return start, end


def _extract_table(section_lines: list[str]) -> tuple[int, int]:
    """Return (table_start, table_end) indices within the section slice."""
    table_start = None
    for idx, line in enumerate(section_lines):
        if _is_table_row(line):
            table_start = idx
            break
    if table_start is None:
        raise MorningPlanError(
            "The morning plan section has no steps table"
        )

    table_end = table_start
    while table_end < len(section_lines) and _is_table_row(section_lines[table_end]):
        table_end += 1
    return table_start, table_end


def _parse_steps(table_lines: list[str], base: str) -> list[dict]:
    if len(table_lines) < 2:
        raise MorningPlanError("The morning plan table is malformed")

    header = _table_cells(table_lines[0])
    separator = table_lines[1].strip()
    if len(header) < 4 or not separator.replace("|", "").replace("-", "").replace(":", "").strip() == "":
        raise MorningPlanError("The morning plan table is malformed")

    data_lines = table_lines[2:]
    if not data_lines:
        raise MorningPlanError("The morning plan table has zero steps")

    steps: list[dict] = []
    for line in data_lines:
        cells = _table_cells(line)
        if len(cells) < 4:
            raise MorningPlanError("The morning plan table has a malformed row")
        description = " | ".join(cells[3:])
        steps.append({
            "number": cells[0],
            "channel": cells[1],
            "time": cells[2],
            "description": _inline_segments(description, base),
        })
    if not steps:
        raise MorningPlanError("The morning plan table has zero steps")
    return steps


def _extract_next_morning(section_lines: list[str], base: str) -> list[list[dict]]:
    heading_index = None
    for idx, line in enumerate(section_lines):
        if line.strip() == _NEXT_MORNING_HEADING:
            heading_index = idx
            break
    if heading_index is None:
        return []

    body: list[str] = []
    for line in section_lines[heading_index + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        body.append(stripped)
    return _paragraphs(body, base)


def parse_morning_plan(readme_text: str, base: str) -> dict:
    """Parse the Standing morning plan section out of a README whose URL is base.

    Raises MorningPlanError when the section, its steps table, or any step row
    is missing. An empty steps list is never a successful result.
    """
    lines = readme_text.splitlines()
    start, end = _find_section_bounds(lines)
    section_lines = lines[start + 1 : end]

    table_start, table_end = _extract_table(section_lines)
    intro_lines = section_lines[:table_start]
    intro = _paragraphs(intro_lines, base)
    steps = _parse_steps(section_lines[table_start:table_end], base)
    next_morning = _extract_next_morning(section_lines[table_end:], base)

    return {
        "intro": intro,
        "steps": steps,
        "next_morning": next_morning,
    }


def _read(path: str) -> str:
    """The file's UTF-8 text, opened exactly as spelled (no pathlib normalization)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def load_hn_snapshots(snapshot_dir: str | Path, day: date) -> list[dict]:
    """Load the two HN job snapshots for a given day.

    A missing file is ``present: False`` — never "nothing new".
    """
    snapshots: list[dict] = []
    for kind in ("freelance", "hiring"):
        filename = f"hn-jobs-{day.isoformat()}-{kind}.txt"
        path = os.path.join(snapshot_dir, filename)
        present, text = True, None
        try:
            text = _read(path)
        except FileNotFoundError:  # governance: allow-silent SF002: shown as "Not found"
            present = False
        except (OSError, UnicodeDecodeError):
            # Any other failure is not "not found": the page shows "Could not
            # read file"; log why for whoever debugs it.
            logger.exception("Could not read HN snapshot %s", path)
        snapshots.append({
            "kind": kind,
            "filename": filename,
            "present": present,
            "text": text,
        })
    return snapshots


def morning_snapshot(today: date | None = None) -> dict:
    """Build the full /api/morning payload from the source README and HN snapshots."""
    # Machine-local date, because hn_jobs.py names its snapshots with date.today().
    display_day = today or date.today()
    try:
        morning = morning_integration()
    except ValueError as exc:  # a broken [morning] section: the page says why
        raise MorningPlanError(str(exc)) from exc
    if morning is None:
        raise MorningPlanError(MORNING_NOT_CONFIGURED.format(path=local_config_path()))
    source = morning.readme
    try:
        readme_text = _read(source)
    except FileNotFoundError as exc:
        raise MorningPlanError(f"Morning plan source not found: {source}") from exc
    except (OSError, UnicodeDecodeError) as exc:  # a directory, no permission, a name too long, not UTF-8
        raise MorningPlanError(f"Morning plan source could not be read: {source}: {exc}") from exc

    plan = parse_morning_plan(readme_text, f"{morning.link_base}/{quote(os.path.basename(source))}")
    return {
        "date": display_day.isoformat(),
        "source": source,
        "intro": plan["intro"],
        "steps": plan["steps"],
        "next_morning": plan["next_morning"],
        "snapshots": load_hn_snapshots(morning.snapshots, display_day) if morning.snapshots else [],
    }
