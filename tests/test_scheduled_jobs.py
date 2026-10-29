"""scripts/scheduled_jobs.py: what is scheduled to run on this Mac (#8377)."""
import json
import os
import plistlib
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scripts import scheduled_jobs as sj

ROOT = Path.home() / "projects"
SPELLINGS = [str(ROOT), "~/projects", "$HOME/projects", "${HOME}/projects"]
FORMS = ["{p}/sub/run.sh", "cd {p} && ./run", "cd {p}&&./run", "cd '{p}'; ./run", 'exec "{p}/bin/x" >log', "(cd {p})|cat"]
NAMES = st.from_regex(rf"\A{sj.PROJECT_NAME}\Z", fullmatch=True)


@given(NAMES, st.sampled_from(SPELLINGS), st.sampled_from(FORMS))
def test_project_of_finds_a_project_name_however_the_path_is_written(name, root, form):
    assert sj.project_of(["/bin/sh", "-c", form.format(p=f"{root}/{name}")], ROOT) == name


@given(st.text())
def test_project_of_returns_nothing_or_a_valid_project_name(text):
    assert re.fullmatch(rf"(?:{sj.PROJECT_NAME})?", sj.project_of([text], ROOT))


def test_project_of_ignores_names_and_paths_outside_the_rule():
    for text in (f"/x{ROOT}/alpha/run", f"{ROOT}x/alpha", f"{ROOT}/_archive/x", f"{ROOT}/Alpha",
                 f"{ROOT}/alpha_x/run", f'sh -c \'{ROOT}/"alpha beta"/run.sh\'', None):
        assert sj.project_of([text], ROOT) == "", text


def test_plist_row_reads_label_settings_and_project(tmp_path):
    root = tmp_path / "projects"
    path = tmp_path / "a.plist"
    path.write_bytes(plistlib.dumps({
        "Label": "com.t.a", "RunAtLoad": True, "StartInterval": 60,
        "StartCalendarInterval": [{"Hour": 7}, {"Hour": 19}],
        "ProgramArguments": ["/bin/bash", "-c", f"cd {root}/alpha/sub && ./run"]}))
    row = sj._plist_row(path, root)
    assert row["label"] == "com.t.a" and row["project"] == "alpha" and row["error"] is None
    assert row["settings"] == (f'{{"ProgramArguments":["/bin/bash","-c","cd {root}/alpha/sub && ./run"],'
                               '"RunAtLoad":true,"StartCalendarInterval":[{"Hour":7},{"Hour":19}],"StartInterval":60}')
    path.write_bytes(plistlib.dumps({"Label": "com.t.b", "Program": "/usr/bin/true", "WorkingDirectory": f"{root}/beta"}))
    assert sj._plist_row(path, root)["project"] == "beta"
    path.write_bytes(plistlib.dumps({"Label": "com.t.c", "Program": f"{root}/gamma/bin/run"}))
    assert sj._plist_row(path, root)["project"] == "gamma"


PLIST_VALUES = st.recursive(st.booleans() | st.integers(-2**63, 2**63 - 1) | st.text() | st.datetimes(),
                            lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3),
                            max_leaves=8)


@given(st.dictionaries(st.text().filter(lambda k: k not in ("Label", "EnvironmentVariables")), PLIST_VALUES))
def test_describe_settings_shows_every_key_but_the_label(plist):
    shown = json.loads(sj.describe_settings({"Label": "x", **plist}))
    assert shown == json.loads(json.dumps(plist, default=str))


def test_describe_settings_shows_environment_names_not_values():
    plist = {"EnvironmentVariables": {"TOKEN": "s3cret", "A": "1"}, "OnDemand": False}
    assert sj.describe_settings(plist) == '{"EnvironmentVariables":["A","TOKEN"],"OnDemand":false}'
    assert "s3cret" not in sj.describe_settings({"EnvironmentVariables": "TOKEN=s3cret"})


def test_unreadable_plist_keeps_its_row_with_the_error(tmp_path):
    path = tmp_path / "com.t.bad.plist"
    for content in (b"not a plist", plistlib.dumps(["a list"])):
        path.write_bytes(content)
        row = sj._plist_row(path, tmp_path)
        assert row["label"] == "com.t.bad" and row["error"].startswith("unreadable plist:")
        assert row["status"] is None
    for content in (plistlib.dumps({}), *(plistlib.dumps({"Label": bad}) for bad in ("", " ", False, 7, ["x"]))):
        path.write_bytes(content)
        row = sj._plist_row(path, tmp_path)
        assert row["label"] == "com.t.bad" and row["error"].startswith("invalid plist:")
        assert row["status"] == "invalid"


def test_listdir_lists_a_readable_directory_sorted(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    for name in ("b.plist", "a.plist"):
        (agents / name).write_bytes(b"")
    errors: list[str] = []
    assert sj._listdir(agents, errors, missing_ok=False) == [agents / "a.plist", agents / "b.plist"]
    assert errors == []


def test_listdir_reports_unreadable_and_missing_directories(tmp_path):
    locked, file = tmp_path / "locked", tmp_path / "file"
    locked.mkdir()
    file.write_bytes(b"")
    locked.chmod(0)
    try:
        errors: list[str] = []
        assert sj._listdir(locked, errors, missing_ok=True) is None
        assert sj._listdir(tmp_path / "gone", errors, missing_ok=True) == []
        assert sj._listdir(file, errors, missing_ok=True) == []
        assert sj._listdir(tmp_path / "gone", errors, missing_ok=False) is None
    finally:
        locked.chmod(0o755)
    assert [e.split(":")[0] for e in errors] == [f"cannot read {locked}", f"cannot read {tmp_path / 'gone'}"]


def _write_plist(path: Path, label: str, **keys) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": label, "Program": "/usr/bin/true", **keys}))


def _loaded(monkeypatch, *labels):
    monkeypatch.setattr(sj, "launchctl_list", lambda: [{"label": x, "pid": None, "last_exit": 78} for x in labels])


def test_launchd_jobs_marks_active_inactive_and_not_installed(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "projects"
    agents = home / "Library" / "LaunchAgents"
    _write_plist(agents / "on.plist", "com.t.on")
    _write_plist(agents / "off.plist", "com.t.off")
    _write_plist(root / "alpha" / "launchd" / "off.plist", "com.t.off")  # installed copy: owner only
    _write_plist(root / "beta" / "launchd" / "off.plist", "com.t.off")
    _write_plist(root / "beta" / "launchd" / "repo.plist", "com.t.repo")
    (root / "beta" / "launchd" / "bad.plist").write_bytes(b"")
    (root / "beta" / "launchd" / "nolabel.plist").write_bytes(plistlib.dumps({}))
    (root / "README.md").write_bytes(b"")
    _loaded(monkeypatch, "com.t.on")
    errors: list[str] = []
    jobs = {(j["label"], j["status"]): j for j in sj.launchd_jobs(home, root, errors)}
    assert errors == []
    assert sorted(jobs) == [("bad", "unknown"), ("com.t.off", "inactive"), ("com.t.on", "active"),
                            ("com.t.repo", "not installed"), ("nolabel", "invalid")]
    assert jobs[("com.t.on", "active")]["last_exit"] == 78
    assert jobs[("com.t.off", "inactive")]["project"] == "alpha"
    assert jobs[("com.t.repo", "not installed")]["project"] == "beta"
    assert jobs[("bad", "unknown")]["error"].startswith("unreadable plist:")


def test_launchd_jobs_matches_only_labels_it_read(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "projects"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "com.t.repo.plist").write_bytes(plistlib.dumps({"Label": ""}))
    _write_plist(root / "beta" / "launchd" / "repo.plist", "com.t.repo")
    _loaded(monkeypatch, "com.t.repo")
    jobs = sj.launchd_jobs(home, root, [])
    assert [(j["label"], j["status"], j["project"], j["last_exit"]) for j in jobs] == [
        ("com.t.repo", "invalid", "", None), ("com.t.repo", "not installed", "beta", None)]
    (home / "Library" / "LaunchAgents" / "com.t.repo.plist").write_bytes(b"not a plist")
    jobs = sj.launchd_jobs(home, root, [])
    assert [(j["label"], j["status"], j["project"], j["last_exit"]) for j in jobs] == [
        ("com.t.repo", "unknown", "", None), ("com.t.repo", "unknown", "beta", None)]
    assert jobs[0]["error"].startswith("unreadable plist:") and jobs[1]["error"] is None


def test_launchd_jobs_reports_read_failures_and_keeps_going(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "projects"
    agents, locked = home / "Library" / "LaunchAgents", tmp_path / "locked"
    _write_plist(agents / "on.plist", "com.t.on")
    _write_plist(root / "beta" / "launchd" / "repo.plist", "com.t.repo")
    locked.mkdir()
    (root / "alpha").symlink_to(locked)

    def fail():
        raise RuntimeError("launchctl exit 5: boom")

    monkeypatch.setattr(sj, "launchctl_list", fail)
    locked.chmod(0)
    try:
        errors: list[str] = []
        jobs = sj.launchd_jobs(home, root, errors)
        assert [(j["label"], j["status"]) for j in jobs] == [("com.t.on", "unknown"), ("com.t.repo", "not installed")]
        agents.chmod(0)
        jobs = sj.launchd_jobs(home, root, errors)
        assert [(j["label"], j["status"]) for j in jobs] == [("com.t.repo", "unknown")]
        assert sj.launchd_jobs(home, tmp_path / "gone", errors) == []
    finally:
        locked.chmod(0o755)
        agents.chmod(0o755)
    assert [e.split(":")[0] for e in errors] == [
        "launchctl list failed", f"cannot read {root / 'alpha' / 'launchd'}",
        "launchctl list failed", f"cannot read {root / 'alpha' / 'launchd'}", f"cannot read {agents}",
        "launchctl list failed", f"cannot read {tmp_path / 'gone'}", f"cannot read {agents}"]


FIELDS = st.from_regex(rf"\A{sj.CRON_FIELD}\Z", fullmatch=True)
GAPS = st.text(" \t", min_size=1, max_size=3)
COMMANDS = st.from_regex(r"\A[^\s#=][^\n]*\Z", fullmatch=True)  # "@daily =x" is a setting, as cron reads it
SCHEDULES = st.one_of(st.sampled_from(["@reboot", "@daily", "@hourly", "@weekly"]),
                      st.tuples(st.lists(FIELDS, min_size=5, max_size=5), GAPS).map(lambda t: t[1].join(t[0])))


@given(SCHEDULES, GAPS, COMMANDS)
def test_parse_crontab_reads_a_job_line_as_its_schedule_and_command(schedule, gap, command):
    [row] = sj.parse_crontab(f"{schedule}{gap}{command}", ROOT)
    assert row["error"] is None and row["status"] == "in crontab"
    assert row["settings"] == " ".join(schedule.split()) and row["label"] == command.strip()


@given(st.from_regex(r"""\A(?:"[^"\n]*"|'[^'\n]*'|[^\s="']+)[ \t]*=[^\n]*\Z""", fullmatch=True))
def test_parse_crontab_skips_environment_settings(line):
    assert sj.parse_crontab(line, ROOT) == []


@given(st.text())
def test_parse_crontab_keeps_a_row_for_every_line_that_is_not_skipped(text):
    rows = sj.parse_crontab(text, ROOT)
    kept = [line for line in text.split("\n")
            if line.strip() and not line.lstrip().startswith("#") and not sj.CRON_ENV.match(line)]
    assert len(rows) == len(kept)
    assert all((row["error"] is None) == bool(row["settings"]) for row in rows)


def test_parse_crontab_rows_and_project(tmp_path):
    text = (f"# note\n\nSHELL=/bin/sh\n@reboot =x\n0 12 * * * cd '{tmp_path}/muffin' && ./run\x0cmore # tag\n"
            "@weekly /usr/bin/true\nbogus line\n")
    rows = sj.parse_crontab(text, tmp_path)
    assert [(r["settings"], r["project"], r["status"]) for r in rows] == [
        ("0 12 * * *", "muffin", "in crontab"), ("@weekly", "", "in crontab"), ("", "", "unknown")]
    assert rows[0]["label"] == f"cd '{tmp_path}/muffin' && ./run\x0cmore # tag"
    assert rows[2]["label"] == "bogus line" and rows[2]["error"] == "not a crontab job, setting or comment"


def test_crontab_jobs_reports_failures_and_treats_no_crontab_as_none(monkeypatch):
    def fake(result):
        def run(*args, **kwargs):
            if isinstance(result, BaseException):
                raise result
            return sj.subprocess.CompletedProcess(args, *(out.encode() if isinstance(out, str) else out
                                                          for out in result))
        monkeypatch.setattr(sj.subprocess, "run", run)

    errors: list[str] = []
    fake((1, "", "crontab: no crontab for owner\n"))
    assert sj.crontab_jobs(ROOT, errors) == [] and errors == []
    fake((0, "@daily /usr/bin/true\n", ""))
    assert [r["settings"] for r in sj.crontab_jobs(ROOT, errors)] == ["@daily"] and errors == []
    for result in ((1, "", "crontab: permission denied"), sj.subprocess.TimeoutExpired("crontab", 15),
                   FileNotFoundError("crontab")):
        fake(result)
        assert sj.crontab_jobs(ROOT, errors) == []
    assert len(errors) == 3 and all(e.startswith("crontab -l failed: ") for e in errors)
    assert "permission denied" in errors[0]


def test_scheduled_jobs_endpoint_returns_jobs_and_errors(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import dashboard.app as dashboard_app

    home, root = tmp_path / "home", tmp_path / "projects"
    _write_plist(home / "Library" / "LaunchAgents" / "on.plist", "com.t.on")
    _write_plist(root / "beta" / "launchd" / "repo.plist", "com.t.repo")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PROJECTS_ROOT", str(root))
    _loaded(monkeypatch, "com.t.on")
    monkeypatch.setattr(sj.subprocess, "run", lambda *a, **k: sj.subprocess.CompletedProcess(a, 2, b"", b"boom"))
    response = TestClient(dashboard_app.app).get("/api/scheduled-jobs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(j["label"], j["status"]) for j in body["jobs"]] == [("com.t.on", "active"), ("com.t.repo", "not installed")]
    assert body["errors"] == ["crontab -l failed: exit 2: boom"]


def test_crontab_jobs_reads_the_real_output_byte_for_byte(tmp_path, monkeypatch):
    """Through a real subprocess: a "\\r" stays inside its line and bad UTF-8 does not fail the read."""
    crontab = tmp_path / "bin" / "crontab"
    crontab.parent.mkdir()
    crontab.write_bytes(b"#!/bin/sh\nprintf '0 1 * * * a\\r b\\n# note\\r@daily /usr/bin/true\\n@daily caf\\351\\n'\n")
    crontab.chmod(0o755)
    monkeypatch.setenv("PATH", f"{crontab.parent}:{os.environ['PATH']}")
    errors: list[str] = []
    rows = sj.crontab_jobs(ROOT, errors)
    assert errors == []
    assert [(r["settings"], r["label"]) for r in rows] == [("0 1 * * *", "a\r b"), ("@daily", "caf\ufffd")]


def test_launchctl_list_reads_the_real_output_byte_for_byte(tmp_path, monkeypatch):
    """Through a real subprocess: a label keeps its "\\r" and bad UTF-8, and a failure raises."""
    launchctl = tmp_path / "bin" / "launchctl"
    launchctl.parent.mkdir()
    launchctl.write_bytes(b"#!/bin/sh\nprintf 'PID\\tStatus\\tLabel\\n-\\t0\\ta\\rb\\n7\\t-\\tcaf\\351\\n'\n")
    launchctl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{launchctl.parent}:{os.environ['PATH']}")
    assert sj.launchctl_list() == [{"label": "a\rb", "pid": None, "last_exit": 0},
                                   {"label": "caf\ufffd", "pid": "7", "last_exit": None}]
    launchctl.write_bytes(b"#!/bin/sh\necho nope >&2; exit 3\n")
    with pytest.raises(RuntimeError, match="launchctl exit 3: nope"):
        sj.launchctl_list()


@given(st.dictionaries(st.text(), PLIST_VALUES))
def test_describe_schedule_always_gives_words(plist):
    words = sj.describe_schedule(plist)
    assert isinstance(words, str) and words


@given(st.integers(0, 59), st.integers(0, 23))
def test_a_daily_time_reads_the_same_from_launchd_and_cron(minute, hour):
    expected = f"daily {hour:02}:{minute:02}"
    assert sj.describe_schedule({"StartCalendarInterval": {"Hour": hour, "Minute": minute}}) == expected
    assert sj.describe_cron(f"{minute} {hour} * * *") == expected


def test_describe_schedule_examples():
    cases = [
        ({"StartInterval": 300, "RunAtLoad": True}, "every 5 min, at login or load"),
        ({"StartInterval": 7200}, "every 2 h"),
        ({"StartInterval": 90}, "every 90 s"),
        ({"StartCalendarInterval": [{"Weekday": 1, "Hour": 9, "Minute": 30}, {"Minute": 15}]},
         "Mon 09:30; at :15 every hour"),
        ({"StartCalendarInterval": {"Month": 1, "Day": 2, "Hour": 3, "Minute": 4}}, "Jan day 2 03:04"),
        ({"StartCalendarInterval": {"Day": 1, "Weekday": 0, "Hour": 0, "Minute": 0}}, "day 1 or Sun 00:00"),
        ({"StartCalendarInterval": {"Hour": 5}}, "every minute from 05:00 to 05:59"),
        ({"StartCalendarInterval": {}}, "every minute"),
        ({"KeepAlive": True, "RunAtLoad": True}, "at login or load, kept running"),
        ({"KeepAlive": {"SuccessfulExit": False}}, 'KeepAlive {"SuccessfulExit": false}'),
        ({"KeepAlive": {"SuccessfulExit": True}}, 'KeepAlive {"SuccessfulExit": true}'),
        ({"KeepAlive": False, "OnDemand": False}, "no schedule recognised; see Full settings"),
        ({"OnDemand": False}, "kept running"),
        ({"LaunchEvents": {"com.apple.iokit.matching": {}}}, "on a system event"),
        ({"WatchPaths": ["/tmp/x"], "Sockets": {"a": {}}, "MachServices": {"b": True}},
         "when a watched path changes, when another program calls it"),
        ({"Program": "/bin/true"}, "no schedule recognised; see Full settings"),
        ({"OnDemand": True}, "no schedule recognised; see Full settings"),
        ({"StartInterval": "300"}, 'StartInterval "300"'),
        ({"StartInterval": True}, "StartInterval true"),
        ({"StartCalendarInterval": {"Hour": 24}}, 'StartCalendarInterval {"Hour": 24}'),
        ({"StartCalendarInterval": []}, "StartCalendarInterval []"),
    ]
    for plist, expected in cases:
        assert sj.describe_schedule(plist) == expected, plist


def test_describe_cron_examples():
    cases = [("@reboot", "at startup"), ("@weekly", "Sun 00:00"), ("*/10 * * * *", "every 10 min"), ("*/45 * * * *", "cron */45 * * * *"), ("*/7 * * * *", "cron */7 * * * *"),
             ("30 9 * * 1", "Mon 09:30"), ("0 0 1 * *", "day 1 00:00"), ("0 12 * * 7", "Sun 12:00"),
             ("0 9-17 * * *", "cron 0 9-17 * * *"), ("*/0 * * * *", "cron */0 * * * *"),
             ("0 25 * * *", "cron 0 25 * * *"), ("0 9 * * MON", "cron 0 9 * * MON")]
    for schedule, expected in cases:
        assert sj.describe_cron(schedule) == expected, schedule


def test_rows_carry_schedule_and_running(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "projects"
    agents = home / "Library" / "LaunchAgents"
    _write_plist(agents / "run.plist", "com.t.run", KeepAlive=True)
    _write_plist(agents / "idle.plist", "com.t.idle", StartInterval=60)
    (agents / "nolabel.plist").write_bytes(plistlib.dumps({"StartInterval": 60}))
    monkeypatch.setattr(sj, "launchctl_list", lambda: [{"label": "com.t.run", "pid": "42", "last_exit": 0},
                                                       {"label": "com.t.idle", "pid": None, "last_exit": 0}])
    jobs = {j["label"]: j for j in sj.launchd_jobs(home, root, [])}
    assert (jobs["com.t.run"]["schedule"], jobs["com.t.run"]["running"]) == ("kept running", True)
    assert (jobs["com.t.idle"]["schedule"], jobs["com.t.idle"]["running"]) == ("every 1 min", False)
    assert (jobs["nolabel"]["schedule"], jobs["nolabel"]["running"]) == ("", False)
    [cron] = sj.parse_crontab("0 12 * * * ./run\n", root)
    assert (cron["schedule"], cron["running"]) == ("daily 12:00", False)


@given(st.integers(1, 120))
def test_a_cron_step_is_an_interval_only_when_its_gaps_are_equal(step):
    minutes = list(range(0, 60, step))
    gaps = {(b - a) % 60 or 60 for a, b in zip(minutes, minutes[1:] + minutes[:1])}
    words = sj.describe_cron(f"*/{step} * * * *")
    assert words.startswith("every ") == (gaps == {step})
