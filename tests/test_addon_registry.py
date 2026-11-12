"""The add-on registry: the core finds add-ons by folder and names none of them.

These tests must pass with addons/ empty; each add-on tests itself in its folder.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from pt import cli
from scripts.addon_registry import ADDONS_DIR, Addon, discover

ROOT = Path(__file__).resolve().parent.parent
ENV = {"PT_NO_BANNER": "1", "PT_SUPPRESS_MIGRATION_WARNING": "1"}


def _fake_addon(tmp_path: Path, monkeypatch, folder: str, addon: Addon) -> None:
    """An add-on folder whose manifest module is already imported."""
    (tmp_path / folder).mkdir()
    (tmp_path / folder / "addon.py").write_text("")
    monkeypatch.setitem(sys.modules, f"addons.{folder}.addon", types.SimpleNamespace(ADDON=addon))


def test_an_empty_or_missing_addons_folder_has_no_add_ons(tmp_path: Path) -> None:
    assert discover(tmp_path) == []
    assert discover(tmp_path / "missing") == []


def test_add_ons_are_found_by_folder(tmp_path: Path, monkeypatch) -> None:
    addon = Addon(name="fake_one", summary="A test add-on")
    _fake_addon(tmp_path, monkeypatch, "fake_one", addon)
    assert discover(tmp_path) == [addon]


def test_a_misnamed_manifest_is_refused(tmp_path: Path, monkeypatch) -> None:
    _fake_addon(tmp_path, monkeypatch, "fake_one", Addon(name="other", summary="x"))
    with pytest.raises(ValueError, match="names itself 'other'"):
        discover(tmp_path)


def test_pt_addons_list_shows_every_add_on_found() -> None:
    listed = CliRunner().invoke(cli, ["addons", "list"], env=ENV)
    assert listed.exit_code == 0, listed.output
    names = sorted(path.parent.name for path in ADDONS_DIR.glob("*/addon.py"))
    first_words = [line.split()[0] for line in listed.output.splitlines()]
    assert first_words == (names or ["No"])


def test_the_core_names_no_add_on() -> None:
    """Only the registry reaches into addons/, and only by folder."""
    core = [
        path for folder in ("scripts", "dashboard")
        for path in (ROOT / folder).rglob("*.py")
        if "node_modules" not in path.parts
    ]
    named = [
        f"{path.relative_to(ROOT)}: {line.strip()}"
        for path in core
        for line in path.read_text().splitlines()
        if re.search(r"\baddons\.[a-z_]", line)
    ]
    assert named == []


def test_the_core_frontend_names_no_add_on() -> None:
    """The core's pages reach addons/ only through the glob in src/addons.ts."""
    src = ROOT / "dashboard" / "frontend" / "src"
    named = [
        f"{path.relative_to(ROOT)}: {line.strip()}"
        for path in src.rglob("*")
        if path.suffix in {".ts", ".tsx", ".js", ".jsx"}
        for line in path.read_text().splitlines()
        if re.search(r"addons/[a-z_]", line)
    ]
    assert named == []


# --- Turning add-ons on and off (#9000) ---------------------------------------

CONFIG = """# hand-kept notes stay
[owner]
name = "Sam"

[graph]
skip_dirs = ["scratch"]  # a trailing comment
"""


@pytest.fixture
def two_addons(tmp_path: Path, monkeypatch):
    """Two made-up add-ons, found by both the registry and `pt addons`."""
    import commands.addons as commands  # the module pt.py imports

    found = [Addon(name="alpha", summary="First"), Addon(name="beta", summary="Second")]
    monkeypatch.setattr(commands, "discover", lambda: found)
    config = tmp_path / "config.toml"
    config.write_text(CONFIG)
    monkeypatch.setenv("PT_CONFIG", str(config))
    return config


def _addons(*args: str):
    return CliRunner().invoke(cli, ["addons", *args], env=ENV)


def test_disable_and_enable_edit_only_the_addons_section(two_addons: Path) -> None:
    assert _addons("disable", "beta").exit_code == 0
    assert two_addons.read_text() == CONFIG + '\n[addons]\ndisabled = ["beta"]\n'
    assert "beta  off" in _addons("list").output and "alpha  on" in _addons("list").output

    assert _addons("disable", "alpha").exit_code == 0
    assert two_addons.read_text() == CONFIG + '\n[addons]\ndisabled = ["alpha", "beta"]\n'
    assert "already off" in _addons("disable", "alpha").output

    assert _addons("enable", "beta").exit_code == 0
    assert _addons("enable", "alpha").exit_code == 0
    assert two_addons.read_text() == CONFIG + "\n[addons]\ndisabled = []\n"
    assert "already on" in _addons("enable", "alpha").output


def test_a_section_in_the_middle_keeps_what_follows(two_addons: Path) -> None:
    two_addons.write_text('[addons]\ndisabled = []\n\n' + CONFIG)
    assert _addons("disable", "alpha").exit_code == 0
    assert two_addons.read_text() == '[addons]\ndisabled = ["alpha"]\n\n' + CONFIG


def test_a_missing_config_file_is_created(two_addons: Path) -> None:
    two_addons.unlink()  # governance: allow-delete DS001: the test's own tmp_path config
    assert _addons("disable", "alpha").exit_code == 0
    assert two_addons.read_text() == '[addons]\ndisabled = ["alpha"]\n'


@pytest.mark.parametrize("section", [
    '[addons]\n# why beta is off\ndisabled = ["beta"]\n',
    '[addons]\ndisabled = [\n  "beta",\n]\n',
])
def test_a_hand_written_section_is_refused_untouched(two_addons: Path, section: str) -> None:
    two_addons.write_text(CONFIG + "\n" + section)
    result = _addons("disable", "alpha")
    assert result.exit_code != 0 and "edit it by hand" in result.output
    assert two_addons.read_text() == CONFIG + "\n" + section


def test_unknown_names_are_refused(two_addons: Path) -> None:
    for action in ("enable", "disable"):
        result = _addons(action, "gamma")
        assert result.exit_code != 0 and "found: alpha, beta" in result.output
    assert two_addons.read_text() == CONFIG


def test_enabled_leaves_out_disabled_add_ons(tmp_path: Path, monkeypatch) -> None:
    from scripts.addon_registry import enabled

    alpha, beta = Addon(name="alpha", summary="a"), Addon(name="beta", summary="b")
    _fake_addon(tmp_path, monkeypatch, "alpha", alpha)
    _fake_addon(tmp_path, monkeypatch, "beta", beta)
    config = tmp_path / "config.toml"
    monkeypatch.setenv("PT_CONFIG", str(config))
    assert enabled(tmp_path) == [alpha, beta]
    config.write_text('[addons]\ndisabled = ["beta"]\n')
    assert enabled(tmp_path) == [alpha]


@pytest.mark.parametrize("bad", ['[addons]\ndisabled = "beta"\n', "[addons]\nenabled = []\n", "[addons\n"])
def test_an_unreadable_addons_section_loads_none_and_says_why(
        tmp_path: Path, monkeypatch, capsys, bad: str) -> None:
    from scripts.addon_registry import enabled

    _fake_addon(tmp_path, monkeypatch, "alpha", Addon(name="alpha", summary="a"))
    config = tmp_path / "config.toml"
    config.write_text(bad)
    monkeypatch.setenv("PT_CONFIG", str(config))
    assert enabled(tmp_path) == []
    assert "add-ons not loaded:" in capsys.readouterr().err


def test_a_bad_config_is_a_message_not_a_traceback(two_addons: Path) -> None:
    two_addons.write_text('[addons]\ndisabled = "beta"\n')
    for args in (("list",), ("disable", "alpha")):
        result = _addons(*args)
        assert result.exit_code == 1 and "disabled must be a list" in result.output
    two_addons.write_text(CONFIG + "\n[addons]")  # a header with no line under it
    result = _addons("disable", "alpha")
    assert result.exit_code == 1 and "would not parse" in result.output
    assert two_addons.read_text() == CONFIG + "\n[addons]"


def test_an_add_on_menu_entry_can_join_a_core_group() -> None:
    from dashboard.common import add_addon_navigation

    items = [{"id": "group-a", "label": "A", "children": [{"id": "core"}]}, {"id": "leaf"}]
    add_addon_navigation(items, [{"id": "joined", "group": "group-a"}, {"id": "alone"}])
    assert items == [
        {"id": "group-a", "label": "A", "children": [{"id": "core"}, {"id": "joined"}]},
        {"id": "leaf"},
        {"id": "alone"},
    ]
    with pytest.raises(ValueError, match="names no menu group 'group-b'"):
        add_addon_navigation(items, [{"id": "lost", "group": "group-b"}])
    with pytest.raises(ValueError, match="names no menu group 'leaf'"):
        add_addon_navigation(items, [{"id": "lost", "group": "leaf"}])
