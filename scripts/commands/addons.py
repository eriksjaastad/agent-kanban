"""pt addons: the features that plug into the core from addons/."""

import json
import os
import re
import tempfile
import tomllib

import click

from scripts.addon_registry import discover
from scripts.config import disabled_addons, local_config, local_config_path

# The one [addons] form these commands write and will rewrite: the header with a
# one-line disabled list right under it. Only that line is ever replaced.
_SECTION = re.compile(r"^\[addons\][ \t]*\n(?P<line>[^\n]*)", re.M)
_DISABLED_LINE = re.compile(r"^disabled[ \t]*=[ \t]*\[[^\n]*\][ \t]*$")


@click.group(name="addons")
def addons_group() -> None:
    """Add-ons: optional features, one folder each under addons/.

    The core (tasks, projects, info, handoff, calendar and install care)
    always runs. Each add-on adds its own pt commands and dashboard pages.
    Add-ons are on unless turned off with `pt addons disable`, which keeps
    their data; deleting the folder removes the feature entirely. Public
    add-ons ship in the public build.
    """


@addons_group.command(name="list")
def addons_list() -> None:
    """Show every add-on found: on or off, public or private, and what it is for."""
    addons = discover()
    off = _off()
    for addon in addons:
        state = "off" if addon.name in off else "on"
        visibility = "public" if addon.public else "private"
        click.echo(f"{addon.name}  {state}  ({visibility})  {addon.summary}")
    if not addons:
        click.echo("No add-ons found in addons/.")
    for name in sorted(off - {addon.name for addon in addons}):
        click.echo(f"{name}  disabled in {local_config_path()}, but not in addons/")


def _off() -> set[str]:
    """The disabled add-ons; a config error is a clean message, not a traceback."""
    try:
        return set(disabled_addons())
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _found(name: str) -> None:
    names = sorted(addon.name for addon in discover())
    if name not in names:
        raise click.BadParameter(
            f"no add-on {name!r} in addons/; found: {', '.join(names) or 'none'}", param_hint="NAME")


def _with_disabled(text: str, names: list[str]) -> str:
    """The config text with its [addons] section holding exactly this disabled list."""
    line = f"disabled = {json.dumps(names)}"
    match = _SECTION.search(text)
    if match is None:  # append a new section after a blank line
        lead = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{lead}[addons]\n{line}\n"
    if not _DISABLED_LINE.match(match["line"]):
        raise click.ClickException(
            f"{local_config_path()}: [addons] is not the one-line form pt writes; edit it by hand")
    return f"{text[:match.start('line')]}{line}{text[match.end('line'):]}"


def _save_disabled(off: set[str]) -> None:
    """Rewrite only [addons] disabled; check the result parses the same elsewhere first."""
    path = local_config_path()
    before = local_config()
    text = path.read_text() if path.exists() else ""
    new_text = _with_disabled(text, sorted(off))
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise click.ClickException(f"{path}: refusing to save, the edit would not parse: {exc}") from exc
    if {k: v for k, v in after.items() if k != "addons"} != {k: v for k, v in before.items() if k != "addons"} \
            or disabled_addons(after) != off:
        raise click.ClickException(f"{path}: refusing to save, the edit would change more than [addons]")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as tmp:
        tmp.write(new_text)
    os.replace(tmp.name, path)


_RESTART = "Restart the dashboard for its pages to follow."


@addons_group.command(name="enable")
@click.argument("name")
def addons_enable(name: str) -> None:
    """Turn an add-on back on: its commands now, its pages after a dashboard restart."""
    _found(name)
    off = _off()
    if name not in off:
        click.echo(f"{name} is already on.")
        return
    _save_disabled(off - {name})
    click.echo(f"Turned {name} on. {_RESTART}")


@addons_group.command(name="disable")
@click.argument("name")
def addons_disable(name: str) -> None:
    """Turn an add-on off; its data stays, and `pt addons enable` brings it back."""
    _found(name)
    off = _off()
    if name in off:
        click.echo(f"{name} is already off.")
        return
    _save_disabled(off | {name})
    click.echo(f"Turned {name} off; its data is kept. {_RESTART}")
