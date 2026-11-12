"""The add-on registry: the core finds add-ons by folder, never by name.

Each folder under addons/ that holds an addon.py is one add-on, and its
module-level ADDON is the manifest. The manifest names what the add-on plugs
into the core as "module:attribute" strings, loaded only by the side that uses
them, so `pt` never imports the dashboard's code and the dashboard never
imports the CLI's. Delete an add-on's folder and its feature is gone; list it
in the local config's [addons] disabled to turn it off and keep its data.

Its React pages live in addons/<name>/frontend/, whose routes.tsx the
dashboard's frontend build finds by itself. A disabled add-on's pages are
still built, but the server no longer serves them.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.config import disabled_addons

ADDONS_DIR = Path(__file__).resolve().parent.parent / "addons"


@dataclass(frozen=True)
class Addon:
    """One add-on's manifest: what it is and what it plugs into the core."""

    name: str  # must match its folder name
    summary: str  # one line for `pt addons list`
    public: bool = False  # ships in the public agent-kanban build
    commands: tuple[str, ...] = ()  # click commands that `pt` adds
    router: str | None = None  # the FastAPI router the dashboard includes
    pages: tuple[str, ...] = ()  # paths the dashboard serves its React app on
    # Menu entries: each goes last in the core group its "group" names (an
    # unknown group is an error), or at the end of the menu when it names none.
    # "solo_label" is its name if it is ever the only item left in its group.
    nav: tuple[dict[str, Any], ...] = ()


def discover(addons_dir: Path = ADDONS_DIR) -> list[Addon]:
    """Every add-on under addons_dir, in folder-name order."""
    found = []
    for manifest in sorted(addons_dir.glob("*/addon.py")):
        folder = manifest.parent.name
        addon = importlib.import_module(f"addons.{folder}.addon").ADDON
        if addon.name != folder:
            raise ValueError(f"addons/{folder}/addon.py names itself {addon.name!r}")
        found.append(addon)
    return found


def enabled(addons_dir: Path = ADDONS_DIR) -> list[Addon]:
    """The add-ons found that the local config does not turn off.

    An [addons] section that can't be read loads none and says why on stderr,
    so the core still runs and the file can be fixed.
    """
    try:
        off = disabled_addons()
    except ValueError as exc:  # governance: allow-silent SF002: reported on stderr; the core must still run to fix the file
        print(f"add-ons not loaded: {exc}", file=sys.stderr)
        return []
    return [addon for addon in discover(addons_dir) if addon.name not in off]


def load(ref: str) -> Any:
    """The object a manifest's "module:attribute" string names."""
    module, _, attribute = ref.partition(":")
    return getattr(importlib.import_module(module), attribute)
