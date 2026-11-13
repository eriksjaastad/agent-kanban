"""The private local config (scripts/config.py): integrations are off unless configured (#8677).

Why these exist: the public showcase ships no knowledge of the owner's other
projects, so every integration path comes from this file, and a mistake in it
must be loud rather than turn an integration off.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from commands import message as message_commands  # noqa: E402
from commands import config as config_commands  # noqa: E402
from scripts import config


# Characters a TOML string can hold, and no "~" (see the tests that use it).
TOML_TEXT = st.characters(blacklist_categories=("Cs",), blacklist_characters="~")


def write(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("PT_CONFIG", str(path))
    return path


def test_default_path_is_under_the_users_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("PT_CONFIG")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.local_config_path() == tmp_path / ".config" / "project-tracker" / "config.toml"


def test_chat_addresses_are_read_and_join_the_projects(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, '[chat]\naddresses = ["architect", "me"]\n')
    monkeypatch.setattr(message_commands, "DatabaseManager", lambda: type("D", (), {"get_all_projects": lambda self: [{"id": "app"}]})())
    assert message_commands._known_chat_addresses() == {"architect", "me", "app"}


@settings(max_examples=150, deadline=None)
@given(addresses=st.lists(st.text(st.characters(blacklist_categories=("Cs",)), max_size=6), max_size=4))
def test_chat_addresses_come_back_exactly_or_the_file_is_named(tmp_path_factory, addresses):
    path = tmp_path_factory.mktemp("chat") / "config.toml"
    def toml_string(text):  # quotes, backslashes and non-printables as \U escapes, valid TOML for any scalar
        return '"' + "".join(c if c.isprintable() and c not in '"\\' else f"\\U{ord(c):08x}" for c in text) + '"'

    path.write_text("[chat]\naddresses = [" + ", ".join(map(toml_string, addresses)) + "]\n", encoding="utf-8")
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if all(a and a == a.strip() for a in addresses):
            assert config.chat_addresses() == set(addresses)
        else:
            with pytest.raises(ValueError, match=r"config\.toml: \[chat\] addresses"):
                config.chat_addresses()


@settings(max_examples=150, deadline=None)
@given(to=st.one_of(st.none(), st.integers(), st.text(max_size=5)), sender=st.text(max_size=5),
       host=st.one_of(st.none(), st.text(max_size=5)), prefixes=st.lists(st.text(max_size=4), max_size=3))
def test_a_digest_section_loads_exactly_or_the_file_is_named(tmp_path_factory, to, sender, host, prefixes):
    def name(value):
        return isinstance(value, str) and value != "" and value == value.strip()

    section = {k: v for k, v in {"to": to, "from": sender, "mini_host": host, "job_label_prefixes": prefixes}.items()
               if v is not None}
    path = tmp_path_factory.mktemp("digest") / "config.toml"
    path.write_text("x = 1\n", encoding="utf-8")
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if name(to) and name(sender) and (host is None or name(host) and not host.startswith("-")) \
                and all(map(name, prefixes)):
            assert config.digest_integration({"digest": section}) == config.DigestIntegration(
                to, sender, tuple(prefixes), host)
        else:
            with pytest.raises(ValueError, match=r"config\.toml: \[digest\]"):
                config.digest_integration({"digest": section})


@settings(max_examples=150, deadline=None)
@given(entries=st.lists(st.fixed_dictionaries(
    {"label": st.text(max_size=4), "exit_code": st.one_of(st.sampled_from([0, 1, 255, 256]), st.integers(-2, 258), st.booleans(),
                          st.text(max_size=2)),
     "report": st.text(max_size=4)}, optional={"extra": st.just(1)}), max_size=3),
    doppler=st.sampled_from([{}, {"doppler_project": "p", "doppler_config": "c"}, {"doppler_project": "p"}]))
@example(entries=[{"label": "l", "exit_code": code, "report": "r"} for code in (0, 1, 255, 256)], doppler={})
@example(entries=[{"label": "l", "exit_code": 255, "report": "r"}], doppler={})
def test_digest_attention_and_doppler_load_exactly_or_name_the_file(tmp_path_factory, entries, doppler):
    def name(value):
        return value != "" and value == value.strip()

    path = tmp_path_factory.mktemp("digest") / "config.toml"
    section = {"to": "a", "from": "b", "attention": entries, **doppler}
    good = len(doppler) != 1 and all(
        set(e) == {"label", "exit_code", "report"} and name(e["label"]) and name(e["report"])
        and type(e["exit_code"]) is int and 1 <= e["exit_code"] <= 255 for e in entries)
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if good:
            digest = config.digest_integration({"digest": section})
            assert digest.attention == {(e["label"], e["exit_code"]): e["report"] for e in entries}
            assert (digest.doppler_project, digest.doppler_config) == (doppler.get("doppler_project"),
                                                                       doppler.get("doppler_config"))
        else:
            with pytest.raises(ValueError, match=r"config\.toml: (each )?\[+digest"):
                config.digest_integration({"digest": section})


@settings(max_examples=150, deadline=None)
@given(ignore=st.one_of(st.none(), st.text(max_size=3), st.lists(st.one_of(st.text(max_size=4), st.integers()), max_size=3)),
       mini_ignore=st.one_of(st.none(), st.lists(st.text(max_size=4), max_size=3)))
@example(ignore=["a", "a"], mini_ignore=[""])
@example(ignore=" a", mini_ignore=[" b"])
def test_digest_ignore_lists_load_exactly_or_name_the_file(tmp_path_factory, ignore, mini_ignore):
    def names(value):
        return value is None or isinstance(value, list) and all(
            isinstance(v, str) and v != "" and v == v.strip() for v in value)

    section = {"to": "a", "from": "b", **{k: v for k, v in {"ignore": ignore, "mini_ignore": mini_ignore}.items()
                                         if v is not None}}
    path = tmp_path_factory.mktemp("digest") / "config.toml"
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if names(ignore) and names(mini_ignore):
            digest = config.digest_integration({"digest": section})
            assert (digest.ignore, digest.mini_ignore) == (frozenset(ignore or ()), frozenset(mini_ignore or ()))
        else:
            with pytest.raises(ValueError, match=r"config\.toml: \[digest\]"):
                config.digest_integration({"digest": section})


@settings(max_examples=150, deadline=None)
# Text TOML can hold: no lone surrogates. A leading "~" is pinned below, since
# "~user" expands differently on every machine.
@given(readme=st.one_of(st.none(), st.integers(), st.text(TOML_TEXT, max_size=5)),
       link_base=st.one_of(st.none(), st.text(TOML_TEXT, max_size=5)),
       snapshots=st.one_of(st.none(), st.text(TOML_TEXT, max_size=5)))
@example(readme="r.md", link_base="https://x.test/", snapshots=None)
@example(readme="r.md", link_base="https://x.test", snapshots=" s")
@example(readme="r.md", link_base="//", snapshots=None)
@example(readme="~/r.md", link_base="https://x.test", snapshots="~/s")
@example(readme="r\0.md", link_base="https://x.test", snapshots=None)
@example(readme="r.md", link_base="x\0", snapshots="s\0")
@example(readme="missing/../r.md", link_base="https://x.test", snapshots="/abs/s")
def test_a_morning_section_loads_exactly_or_the_file_is_named(tmp_path_factory, readme, link_base, snapshots):
    def name(value, path=True):
        return isinstance(value, str) and value != "" and value == value.strip() and not (path and "\0" in value)

    section = {k: v for k, v in {"readme": readme, "link_base": link_base, "snapshots": snapshots}.items()
               if v is not None}
    path = tmp_path_factory.mktemp("morning") / "config.toml"
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if name(readme) and name(link_base, path=False) and link_base.rstrip("/") and (
                snapshots is None or name(snapshots)):
            morning = config.morning_integration({"morning": section})
            assert (morning.link_base, morning.snapshots is None) == (link_base.rstrip("/"), snapshots is None)
        else:
            with pytest.raises(ValueError, match=str(path)):
                config.morning_integration({"morning": section})


def test_an_unknown_home_directory_is_a_config_error(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, '[morning]\nreadme = "~no-such-user-8677/plan.md"\nlink_base = "https://x.test"\n')
    with pytest.raises(ValueError, match=f"{path}: \\[morning\\] readme .* no such user"):
        config.morning_integration()


def _open_outcome(where):
    """What opening a path gives: its text, or the kind of OS error."""
    try:
        with open(where, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        return type(exc).__name__


@settings(max_examples=200, deadline=None)
@given(parts=st.lists(st.sampled_from(["plan.md", "dir", "missing", ".", "..", ""]), min_size=1, max_size=4),
       trailing=st.sampled_from(["", "/", "/.", "/./", "//"]), absolute=st.booleans())
@example(parts=["plan.md"], trailing="/", absolute=False)
@example(parts=["plan.md"], trailing="/.", absolute=False)
@example(parts=["missing", "..", "plan.md"], trailing="", absolute=False)
@example(parts=["dir", "plan.md"], trailing="", absolute=True)
def test_a_morning_path_opens_exactly_as_the_configured_spelling(tmp_path_factory, parts, trailing, absolute):
    """The page opens the readme just as the OS would open the configured string from the config's directory."""
    root = tmp_path_factory.mktemp("morning")
    (root / "plan.md").write_text("plan")
    (root / "dir").mkdir()
    (root / "dir" / "plan.md").write_text("inner")
    raw = "/".join(parts) + trailing
    if absolute:
        raw = f"{root}/{raw}"
    if not raw:
        return
    path = root / "config.toml"
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        readme = config.morning_integration({"morning": {"readme": raw, "link_base": "https://x.test"}}).readme
    cwd = os.getcwd()
    os.chdir(root)
    try:
        expected = _open_outcome(raw)
    finally:
        os.chdir(cwd)
    assert _open_outcome(readme) == expected


@settings(max_examples=100, deadline=None)
@given(section=st.one_of(st.integers(), st.dictionaries(st.sampled_from(["tailor_rules", "rules"]), st.one_of(
    st.integers(), st.text(TOML_TEXT, max_size=5)), max_size=2)))
@example(section={"tailor_rules": "r.md"})
@example(section={"tailor_rules": ""})
@example(section={"tailor_rules": "r\0"})
@example(section={"tailor_rules": "rules.md/"})
def test_tailor_rules_load_exactly_or_the_file_is_named(tmp_path_factory, section):
    path = tmp_path_factory.mktemp("jobs") / "config.toml"
    rules = section.get("tailor_rules") if isinstance(section, dict) else None
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if isinstance(section, dict) and set(section) <= {"tailor_rules"} and (rules is None or (
                isinstance(rules, str) and rules and rules == rules.strip() and "\0" not in rules)):
            # Placed beside the config, spelled as written (see test_a_morning_path_opens_exactly_...).
            assert config.tailor_rules({"jobs": section}) == (rules and os.path.join(str(path.parent), rules))
        else:
            with pytest.raises(ValueError, match=str(path)):
                config.tailor_rules({"jobs": section})


@settings(max_examples=100, deadline=None)
@given(section=st.one_of(st.integers(), st.dictionaries(st.sampled_from(["fit_keywords", "tailor_rules", "keywords"]),
    st.one_of(st.text(TOML_TEXT, max_size=3), st.lists(st.one_of(st.integers(), st.text(TOML_TEXT, max_size=4)),
                                                       max_size=3)), max_size=2)))
@example(section={"fit_keywords": ["react", "front end"]})
@example(section={"fit_keywords": [" react"]})
@example(section={"fit_keywords": [""]})
@example(section={"keywords": ["react"]})
def test_fit_keywords_load_exactly_or_name_the_file(tmp_path_factory, section):
    path = tmp_path_factory.mktemp("jobs") / "config.toml"
    words = section.get("fit_keywords", []) if isinstance(section, dict) else None
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if isinstance(section, dict) and set(section) <= {"fit_keywords", "tailor_rules"} and isinstance(
                words, list) and all(isinstance(w, str) and w and w == w.strip() for w in words):
            assert config.fit_keywords({"jobs": section}) == tuple(words)
        else:
            with pytest.raises(ValueError, match=r"config\.toml: \[jobs\]"):
                config.fit_keywords({"jobs": section})


@settings(max_examples=150, deadline=None)
@given(section=st.one_of(st.integers(), st.dictionaries(
    st.sampled_from(["skip_dirs", "ignore_projects", "protected_projects"]),
    st.one_of(st.text(TOML_TEXT, max_size=3), st.lists(st.one_of(st.integers(), st.text(TOML_TEXT, max_size=4)),
                                                       max_size=3)), max_size=2)))
@example(section={"skip_dirs": ["a", "a"], "ignore_projects": []})
@example(section={"skip_dirs": [" a"]})
@example(section={"ignore_projects": [""]})
def test_graph_settings_load_exactly_or_name_the_file(tmp_path_factory, section):
    def names(value):
        return isinstance(value, list) and all(isinstance(v, str) and v and v == v.strip() for v in value)

    path = tmp_path_factory.mktemp("graph") / "config.toml"
    with patch.dict(os.environ, {"PT_CONFIG": str(path)}):
        if isinstance(section, dict) and set(section) <= {"skip_dirs", "ignore_projects"} and all(
                names(v) for v in section.values()):
            assert config.graph_settings({"graph": section}) == (
                frozenset(section.get("skip_dirs", [])), frozenset(section.get("ignore_projects", [])))
        else:
            with pytest.raises(ValueError, match=r"config\.toml: \[graph\]"):
                config.graph_settings({"graph": section})


def test_config_reports_the_repo_root_as_project_root():
    """`pt config` names the checkout pt runs from, not its scripts/ folder."""
    root = Path(config_commands.__file__).resolve().parents[2]
    assert (root / "pt").is_file()
    assert config_commands._effective_config_payload()["project_root"] == str(root)


def test_owner_name_defaults_to_owner_and_rejects_bad_values():
    """#8961: [owner] name is the person the board waits on; its prefix is the name in capitals."""
    from scripts.config import owner_name, owner_prefix

    assert owner_name({}) == "Owner" and owner_prefix("Owner") == "OWNER:"
    assert owner_name({"owner": {"name": "Robin"}}) == "Robin" and owner_prefix("Robin") == "ROBIN:"
    for bad in ({"owner": {"name": ""}}, {"owner": {"name": " Robin"}}, {"owner": {"name": "A:B"}},
                {"owner": {"name": 3}}, {"owner": {"prefix": "X:"}}, {"owner": "Robin"}):
        with pytest.raises(ValueError, match=r"\[owner\]"):
            owner_name(bad)


DISCORD = {"channel_id": "123", "doppler_project": "bots", "doppler_config": "prd", "token_secret": "TOKEN"}


def discord_toml(**changes):
    values = {**DISCORD, **changes}
    return "[discord]\n" + "".join(f'{k} = "{v}"\n' for k, v in values.items() if v is not None)


def test_a_discord_section_loads_exactly_and_an_absent_one_is_off(tmp_path, monkeypatch):
    assert config.discord_integration() is None
    write(tmp_path, monkeypatch, discord_toml())
    assert config.discord_integration() == config.DiscordIntegration(*DISCORD.values())


@pytest.mark.parametrize("changes", [
    {"channel_id": "12a"}, {"channel_id": "-1"}, {"channel_id": "\u0663"}, {"channel_id": ""},
    {"channel_id": " 12"}, {"token_secret": None}, {"doppler_project": None}, {"doppler_config": ""},
    {"extra": "x"},
])
def test_a_bad_discord_section_names_the_file(tmp_path, monkeypatch, changes):
    write(tmp_path, monkeypatch, discord_toml(**changes))
    with pytest.raises(ValueError, match=r"config\.toml: \[discord\]"):
        config.discord_integration()
