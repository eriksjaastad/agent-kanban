"""The private local config (scripts/config.py): integrations are off unless configured (#8677).

Why these exist: the public showcase ships no knowledge of the owner's other
projects, so every integration path comes from this file, and a mistake in it
must be loud rather than turn an integration off.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from hypothesis import example, given, settings
from hypothesis import strategies as st

import pt
from scripts import config


# Characters a TOML string can hold, and no "~" (see the tests that use it).
TOML_TEXT = st.characters(blacklist_categories=("Cs",), blacklist_characters="~")


def write(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("PT_CONFIG", str(path))
    return path


def test_no_file_means_no_integrations(tmp_path, monkeypatch):
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "absent.toml"))
    assert config.local_config() == {} and config.memory_integration() is None


def test_default_path_is_under_the_users_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("PT_CONFIG")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.local_config_path() == tmp_path / ".config" / "project-tracker" / "config.toml"


def test_memory_section_is_read(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, '[memory]\nroot = "~/store"\ndoppler_project = "mem"\ndoppler_config = "dev"\n')
    memory = config.memory_integration()
    assert memory.root == config.Path("~/store").expanduser()
    assert (memory.brain_py.name, memory.brain_db.name) == ("brain.py", "brain.db")
    assert (memory.doppler_project, memory.doppler_config) == ("mem", "dev")


@pytest.mark.parametrize("text, message", [
    ("[memory\nroot = 1", "not valid TOML"),
    (b"\xff[memory]", "not valid TOML"),
    ("memory = 1\n", "may only set"),
    ('[memory]\nroot = "x"\ntypo = "y"\n', "may only set"),
    ("[memory]\n", "root is required"),
    ('[memory]\nroot = ""\n', "root is required"),
    ("[memory]\nroot = 3\n", "root is required"),
    ('[memory]\nroot = "x"\ndoppler_project = "mem"\n', "go together"),
])
def test_a_bad_file_is_loud(tmp_path, monkeypatch, text, message):
    path = tmp_path / "config.toml"
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    monkeypatch.setenv("PT_CONFIG", str(path))
    with pytest.raises(ValueError, match=message):
        config.memory_integration()


def test_chat_addresses_are_read_and_join_the_projects(tmp_path, monkeypatch):
    write(tmp_path, monkeypatch, '[chat]\naddresses = ["architect", "me"]\n')
    monkeypatch.setattr(pt, "DatabaseManager", lambda: type("D", (), {"get_all_projects": lambda self: [{"id": "app"}]})())
    assert pt._known_chat_addresses() == {"architect", "me", "app"}


@pytest.mark.parametrize("text", ["chat = 1\n", '[chat]\nnames = ["a"]\n', '[chat]\naddresses = "a"\n',
                                  "[chat]\naddresses = [1]\n", '[chat]\naddresses = [""]\n', '[chat]\naddresses = [" a"]\n'])
def test_a_bad_chat_section_refuses_the_send(tmp_path, monkeypatch, text):
    write(tmp_path, monkeypatch, text)
    monkeypatch.setenv("PT_CALLER_CWD", str(tmp_path))
    with patch("pt._load_chat_config", return_value={"url": "u", "key": "k", "sender": "me"}), \
         patch("pt._chat_api_request") as request:
        result = CliRunner().invoke(pt.cli, ["message", "send", "hi", "--to", "architect"])
    assert result.exit_code != 0 and "[chat]" in result.output
    request.assert_not_called()


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


@pytest.mark.parametrize("addresses, args, sent", [
    (None, ["--to", "ops"], False),
    *[('["ops"]', args, True) for args in (["--to", "ops"], ["--to", "ops", "--machine", "mini"], ["--to", "ops@mini"])],
    ('["ops"]', ["--to", "other@mini"], False),
    *[('["ops@mini", "lead"]', args, True) for args in (["--to", "ops@mini"], ["--to", "ops", "--machine", "mini"],
                                                         ["--to", "lead"], ["--to", "lead@laptop"])],
    *[('["ops@mini", "lead"]', args, False) for args in (["--to", "ops"], ["--to", "ops@laptop"],
                                                          ["--to", "ops", "--machine", "laptop"])],
])
def test_a_send_is_checked_even_when_nothing_is_reachable_but_the_config(tmp_path, monkeypatch, addresses, args, sent):
    """No projects is a real answer, not "cannot verify". A bare name reaches any machine, name@machine one."""
    write(tmp_path, monkeypatch, f"[chat]\naddresses = {addresses}\n" if addresses else "")
    monkeypatch.setenv("PT_CALLER_CWD", str(tmp_path))
    monkeypatch.setattr(pt, "DatabaseManager", lambda: type("D", (), {"get_all_projects": lambda self: []})())
    with patch("pt._load_chat_config", return_value={"url": "u", "key": "k", "sender": "me"}), \
         patch("pt._chat_api_request", return_value={"id": 1}) as request:
        result = CliRunner().invoke(pt.cli, ["message", "send", "hi", *args])
    assert (result.exit_code == 0) is sent, result.output
    assert request.called is sent


@pytest.mark.parametrize("text, problem", [
    ('[chat]\naddresses = ["ops", "lead@mini"]\n[digest]\nto = "a@example"\nfrom = "b@example"\n', None),
    ("", None), ("chat = 1\n", "[chat]"), ('[chat]\naddresses = [" ops"]\n', "[chat]"),
    ('[digest]\nto = "a@example"\n', "[digest]"), ('[digest]\nto = "a"\nfrom = "b"\nmini_host = "-oX"\n', "[digest]"),
    ('[morning]\nreadme = "plan.md"\n', "[morning]"), ('[jobs]\ntailor_rules = 1\n', "[jobs]"), ('[jobs]\nfit_keywords = "react"\n', "[jobs]"),
    ('[graph]\nskip_dirs = "scratch"\n', "[graph]"),
])
def test_diagnostics_check_every_section(tmp_path, monkeypatch, text, problem):
    ok = problem is None
    import json

    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    path = write(tmp_path, monkeypatch, text)
    shown = json.loads(CliRunner().invoke(pt.cli, ["config", "show", "--effective", "--json"]).output)
    doctor = json.loads(CliRunner().invoke(pt.cli, ["doctor", "--json"]).output)
    check = next(c for c in doctor["checks"] if c["name"] == "local_config")
    assert shown["ok"] is check["ok"] is ok
    assert (shown["local_config_error"] is None) is ok and (ok or problem in check["detail"])
    assert check["detail"] == str(path) if ok else str(path) in check["detail"]


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


def test_pt_memory_says_how_to_turn_it_on_when_off(tmp_path, monkeypatch):
    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    result = CliRunner().invoke(pt.cli, ["memory", "stats"])
    assert result.exit_code != 0
    assert "memory integration is not configured" in result.output
    assert str(tmp_path / "_local" / "config.toml") in result.output


def test_brain_runs_without_doppler_when_none_is_configured(tmp_path, monkeypatch):
    (tmp_path / "brain.py").write_text("")
    write(tmp_path, monkeypatch, f'[memory]\nroot = "{tmp_path}"\n')
    seen = []
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, check, cwd: seen.append((cmd, cwd)) or type("R", (), {"returncode": 0})())
    pt._run_brain("stats")
    assert seen == [(["uv", "run", str(tmp_path / "brain.py"), "stats"], str(tmp_path))]


def test_brain_runs_under_the_configured_doppler_project(tmp_path, monkeypatch):
    (tmp_path / "brain.py").write_text("")
    write(tmp_path, monkeypatch, f'[memory]\nroot = "{tmp_path}"\ndoppler_project = "mem"\ndoppler_config = "dev"\n')
    monkeypatch.setattr(pt.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen = []
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, check, cwd: seen.append(cmd) or type("R", (), {"returncode": 0})())
    pt._run_brain("stats")
    assert seen == [["doppler", "run", "--project", "mem", "--config", "dev", "--",
                     "uv", "run", str(tmp_path / "brain.py"), "stats"]]


def test_doctor_counts_an_unconfigured_memory_store_as_fine(monkeypatch):
    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    result = CliRunner().invoke(pt.cli, ["doctor", "--json"])
    check = next(c for c in __import__("json").loads(result.output)["checks"] if c["name"] == "memory_readonly")
    assert check["ok"] is True and check["detail"].startswith("not configured (optional")


def test_skill_stats_say_how_to_turn_memory_on_when_off(monkeypatch):
    import skill_invocations_reader as reader

    monkeypatch.setattr(reader, "DEFAULT_BRAIN_DB", None)
    with pytest.raises(FileNotFoundError, match="memory integration is not configured"):
        list(reader.iter_invocations())


def test_skill_stats_read_the_configured_store(tmp_path, monkeypatch):
    import skill_invocations_reader as reader

    monkeypatch.setattr(reader, "DEFAULT_BRAIN_DB", None)
    write(tmp_path, monkeypatch, f'[memory]\nroot = "{tmp_path}"\n')
    with pytest.raises(FileNotFoundError, match=str(tmp_path / "brain.db")):
        list(reader.iter_invocations())


def test_pt_memory_json_reports_not_configured_as_backend_unavailable(monkeypatch):
    import json

    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    result = CliRunner().invoke(pt.cli, ["memory", "stats", "--json"])
    payload = json.loads(result.output)
    assert result.exit_code == pt.EXIT_BACKEND_UNAVAILABLE
    assert payload["error"]["class"] == "backend_unavailable"
    assert "memory integration is not configured" in payload["error"]["message"]


@pytest.mark.parametrize("root, expected", [
    ("{abs}", lambda tmp, cfg: tmp / "store"),
    ("~/store", lambda tmp, cfg: tmp / "home" / "store"),
    ("store", lambda tmp, cfg: cfg / "store"),
    ("../store", lambda tmp, cfg: tmp / "store"),
    ("./sub/../store", lambda tmp, cfg: cfg / "store"),
])
def test_every_root_form_resolves_to_one_absolute_dir_brain_runs_in(tmp_path, monkeypatch, root, expected):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "config.toml").write_text(f'[memory]\nroot = "{root.format(abs=tmp_path / "store")}"\n')
    monkeypatch.setenv("PT_CONFIG", str(cfg / "config.toml"))
    want = expected(tmp_path, cfg).resolve()
    want.mkdir(parents=True, exist_ok=True)
    (want / "brain.py").write_text("")
    assert config.memory_integration().root == want
    seen = []
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, check, cwd: seen.append((cmd, cwd)) or type("R", (), {"returncode": 0})())
    pt._run_brain("stats")
    assert seen == [(["uv", "run", str(want / "brain.py"), "stats"], str(want))]


BAD_CONFIGS = ["[memory\nroot = 1", "[memory]\nroot = 3\n"]


@pytest.mark.parametrize("text", BAD_CONFIGS)
@pytest.mark.parametrize("args, channel", [
    (["memory", "search", "x", "--json"], "stdout"),
    (["memory", "recent", "--json"], "stdout"),
    (["memory", "stats", "--json"], "stdout"),
    (["memory", "export"], "stderr"),
])
def test_a_bad_config_is_a_structured_json_error(tmp_path, monkeypatch, text, args, channel):
    import json

    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    write(tmp_path, monkeypatch, text)
    result = CliRunner().invoke(pt.cli, args)
    payload = json.loads(result.stdout if channel == "stdout" else result.stderr)
    assert result.exit_code == pt.EXIT_VALIDATION
    assert payload["ok"] is False and payload["error"]["class"] == "validation"
    assert str(tmp_path / "config.toml") in payload["error"]["message"]


@pytest.mark.parametrize("text", BAD_CONFIGS)
def test_a_bad_config_fails_doctor_and_config_show_with_the_reason(tmp_path, monkeypatch, text):
    import json

    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    path = write(tmp_path, monkeypatch, text)
    doctor = json.loads(CliRunner().invoke(pt.cli, ["doctor", "--json"]).output)
    check = next(c for c in doctor["checks"] if c["name"] == "memory_readonly")
    assert check["ok"] is False and str(path) in check["detail"]
    shown = pt._effective_config_payload()
    assert shown["ok"] is False and str(path) in shown["local_config_error"] and shown["memory_db_path"] is None


@pytest.mark.parametrize("text", BAD_CONFIGS)
@pytest.mark.parametrize("args", [["memory", "stats"], ["skills", "stats"]])
def test_a_bad_config_is_a_clean_cli_error(tmp_path, monkeypatch, text, args):
    import skill_invocations_reader as reader

    monkeypatch.setattr(reader, "DEFAULT_BRAIN_DB", None)
    path = write(tmp_path, monkeypatch, text)
    result = CliRunner().invoke(pt.cli, args)
    assert result.exit_code == 1 and str(path) in result.output
    assert not isinstance(result.exception, ValueError)


def test_one_command_reads_the_config_once(tmp_path, monkeypatch):
    """The path a command reports is the one it opened, even if the config changes meanwhile."""
    import json
    import sqlite3

    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    stores = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        db = sqlite3.connect(tmp_path / name / "brain.db")
        db.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, uuid TEXT, content TEXT, metadata TEXT, "
                   "scope TEXT, agent_family TEXT, created_at TEXT, source_machine TEXT)")
        db.commit()
        db.close()
        stores.append(config.MemoryIntegration(tmp_path / name))
    calls = iter(stores)
    monkeypatch.setattr(pt, "memory_integration", lambda: next(calls))
    result = CliRunner().invoke(pt.cli, ["memory", "recent", "--json"])
    assert json.loads(result.output)["backend"]["path"] == (tmp_path / "a" / "brain.db").as_posix()



def test_a_directory_or_unreadable_config_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PT_CONFIG", str(tmp_path))
    with pytest.raises(ValueError, match="can't be read"):
        config.memory_integration()
    locked = write(tmp_path, monkeypatch, '[memory]\nroot = "x"\n')
    locked.chmod(0)
    try:
        with pytest.raises(ValueError, match="can't be read"):
            config.memory_integration()
    finally:
        locked.chmod(0o600)


def test_an_unknown_user_in_pt_config_is_a_config_error(monkeypatch):
    monkeypatch.setenv("PT_CONFIG", "~no-such-user-8677/config.toml")
    assert config.local_config_path() == config.Path("~no-such-user-8677/config.toml")
    with pytest.raises(ValueError, match="can't be expanded"):
        config.memory_integration()


BROKEN_CONFIG_PATHS = ["~no-such-user-8677/config.toml", "{tmp}", "{tmp}/bad.toml", "."]


@pytest.mark.parametrize("override", [None, "{tmp}/memory.db"])
@pytest.mark.parametrize("pt_config", BROKEN_CONFIG_PATHS)
def test_diagnostics_report_a_broken_config_instead_of_crashing(tmp_path, monkeypatch, pt_config, override):
    """config show and doctor always answer: with the error for a broken config, even with a DB override."""
    import json

    (tmp_path / "bad.toml").write_text("[memory\n")
    monkeypatch.setenv("PT_CONFIG", pt_config.format(tmp=tmp_path))
    if override:
        monkeypatch.setenv("PT_MEMORY_DB_PATH", override.format(tmp=tmp_path))
    else:
        monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    shown = CliRunner().invoke(pt.cli, ["config", "show", "--effective", "--json"])
    assert shown.exception is None or isinstance(shown.exception, SystemExit), shown.output
    payload = json.loads(shown.output)
    assert payload["ok"] is False and payload["local_config_error"]
    assert payload["memory_db_path"] == (override.format(tmp=tmp_path) if override else None)
    doctor = CliRunner().invoke(pt.cli, ["doctor", "--json"])
    assert doctor.exception is None or isinstance(doctor.exception, SystemExit), doctor.output
    json.loads(doctor.output)
    text = CliRunner().invoke(pt.cli, ["config", "show", "--effective"])
    assert text.exception is None or isinstance(text.exception, SystemExit), text.output


@settings(max_examples=300, deadline=None)
@given(pt_config=st.text(min_size=1).filter(lambda s: "\x00" not in s))
def test_any_pt_config_normalises_without_raising_or_reading(tmp_path_factory, pt_config):
    """local_config_path() never raises, never reads, and a relative value lands under the caller's dir."""
    import os

    home, caller = tmp_path_factory.mktemp("home"), tmp_path_factory.mktemp("caller")
    saved = {k: os.environ.get(k) for k in ("PT_CONFIG", "HOME", "PT_CALLER_CWD")}
    os.environ.update(PT_CONFIG=pt_config, HOME=str(home), PT_CALLER_CWD=str(caller))
    try:
        path = config.local_config_path()
        if not pt_config.strip():
            assert path == home / ".config" / "project-tracker" / "config.toml"
        elif not path.parts or not path.parts[0].startswith("~"):
            assert path.is_absolute()
            if not config.Path(pt_config.strip()).expanduser().is_absolute():
                assert path.is_relative_to(caller)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.mark.parametrize("blank", ["", " ", "\t\n"])
def test_a_blank_pt_config_uses_the_default_under_home_and_nothing_else(tmp_path, monkeypatch, blank):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PT_CONFIG", blank)
    assert config.local_config_path() == tmp_path / ".config" / "project-tracker" / "config.toml"
    assert config.local_config() == {} and config.memory_integration() is None


def test_a_relative_pt_config_is_relative_to_where_pt_was_run(tmp_path):
    """Through the real launcher, run from a directory that is not the checkout."""
    import json
    import os
    import subprocess

    caller = tmp_path / "caller"
    (caller / "store").mkdir(parents=True)
    (caller / "config.toml").write_text('[memory]\nroot = "store"\n')
    launcher = config.PROJECT_ROOT / "pt"
    env = {**os.environ, "PT_CONFIG": "./config.toml", "PT_DB_PATH": os.environ["PT_DB_PATH"]}
    env.pop("PT_MEMORY_DB_PATH", None)
    result = subprocess.run([str(launcher), "config", "show", "--effective", "--json"], cwd=caller, env=env,
                            capture_output=True, text=True, timeout=60)
    payload = json.loads(result.stdout)
    assert payload["local_config_path"] == str(caller / "config.toml")
    assert payload["memory_db_path"] == str((caller / "store" / "brain.db").resolve())


@pytest.mark.parametrize("args", [["memory", "stats"], ["memory", "stats", "--json"], ["doctor", "--json"]])
def test_an_unreadable_config_reaches_the_cli_as_a_config_error(tmp_path, monkeypatch, args):
    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    monkeypatch.setenv("PT_CONFIG", str(tmp_path))
    result = CliRunner().invoke(pt.cli, args)
    assert str(tmp_path) in result.output and "can't be read" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


@settings(max_examples=200, deadline=None)
@given(root=st.text(min_size=1))
def test_any_root_resolves_or_raises_a_config_error(tmp_path_factory, root):
    """Whatever a [memory] root holds, loading it gives an absolute path or a ValueError naming the file."""
    import os

    path = tmp_path_factory.mktemp("cfg") / "config.toml"
    path.write_bytes(b"[memory]\nroot = " + __import__("json").dumps(root).encode("utf-8", "surrogatepass") + b"\n")
    os.environ["PT_CONFIG"] = str(path)
    try:
        memory = config.memory_integration()
    except ValueError as exc:
        assert str(path) in str(exc)
    else:
        assert memory.root.is_absolute()
    finally:
        os.environ.pop("PT_CONFIG")


def test_an_unknown_user_in_the_root_is_a_config_error(tmp_path, monkeypatch):
    path = write(tmp_path, monkeypatch, '[memory]\nroot = "~no-such-user-8677/store"\n')
    with pytest.raises(ValueError, match="can't be resolved") as exc:
        config.memory_integration()
    assert str(path) in str(exc.value)



def test_config_show_reads_the_config_once(tmp_path, monkeypatch):
    """Health of every section and the memory path come from one read: a second would raise StopIteration."""
    monkeypatch.delenv("PT_MEMORY_DB_PATH", raising=False)
    reads = iter([{"memory": {"root": str(tmp_path / "a")}, "chat": {"addresses": ["ops"]}}])
    monkeypatch.setattr(pt, "local_config", lambda: next(reads))
    payload = pt._effective_config_payload()
    assert (payload["ok"], payload["local_config_error"]) == (True, None)
    assert payload["memory_db_path"] == str(tmp_path / "a" / "brain.db")


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
