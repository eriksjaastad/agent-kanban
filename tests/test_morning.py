from datetime import date

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.morning import (
    MorningPlanError,
    load_hn_snapshots,
    morning_snapshot,
    parse_morning_plan,
)


LINK_BASE = "https://example.test/plans/blob/main"
README_URL = f"{LINK_BASE}/README.md"

SYNTHETIC_README = """\
# plans

Preamble that should be ignored.

## Standing morning plan

**Plan intro.** Aim for a calm morning.

Read [the tracker](./applications/TRACKER.md) and `pt tasks -p plans`.

| # | Channel | Time | What happens |
|---|---|---|---|
| 0 | Replies | 2 min | Check [replies](applications/TRACKER.md#replies) first. |
| 1 | Hacker News | 2 min | Open `hn-jobs-*-freelance.txt` snapshots. |
| 2 | Upwork | 10 min | **Screen** new posts and show up to five. |

Every lead shown carries its URL.

### Next morning

Run the table above. First review is Monday.

No new plan written yet.

## Layout

| Path | What |
|---|---|
| resume/ | Master resume |
"""


def test_parse_morning_plan_returns_intro_steps_and_next_morning():
    plan = parse_morning_plan(SYNTHETIC_README, README_URL)

    assert len(plan["intro"]) == 2
    assert plan["intro"][0][0] == {"type": "strong", "text": "Plan intro."}
    intro_second = plan["intro"][1]
    assert {"type": "code", "text": "pt tasks -p plans"} in intro_second
    link = next(seg for seg in intro_second if seg["type"] == "link")
    assert link == {
        "type": "link",
        "text": "the tracker",
        "href": f"{LINK_BASE}/applications/TRACKER.md",
    }

    assert [step["number"] for step in plan["steps"]] == ["0", "1", "2"]
    assert plan["steps"][0]["channel"] == "Replies"
    assert plan["steps"][0]["time"] == "2 min"

    replies_link = next(
        seg for seg in plan["steps"][0]["description"] if seg["type"] == "link"
    )
    assert replies_link["href"] == (
        f"{LINK_BASE}/applications/TRACKER.md#replies"
    )
    assert {
        "type": "code",
        "text": "hn-jobs-*-freelance.txt",
    } in plan["steps"][1]["description"]
    assert {"type": "strong", "text": "Screen"} in plan["steps"][2]["description"]

    assert len(plan["next_morning"]) == 2
    assert plan["next_morning"][0][0]["text"] == (
        "Run the table above. First review is Monday."
    )


def test_parse_morning_plan_absolute_and_mailto_links_unchanged():
    text = """\
## Standing morning plan

See [Contra](https://contra.com/) or [mail](mailto:admin@example).

| # | Channel | Time | What happens |
|---|---|---|---|
| 0 | Replies | 2 min | [A](https://example.com/a) and [B](mailto:b@example). |
"""
    plan = parse_morning_plan(text, README_URL)

    assert plan["intro"][0][1]["href"] == "https://contra.com/"
    assert plan["intro"][0][3]["href"] == "mailto:admin@example"
    hrefs = {
        seg["href"] for seg in plan["steps"][0]["description"] if seg["type"] == "link"
    }
    assert hrefs == {"https://example.com/a", "mailto:b@example"}


@pytest.mark.parametrize(
    "text",
    [
        "# plans\n\nNo morning section here.\n",
        "## Standing morning plan\n\nJust some intro, no table in sight.\n",
        "## Standing morning plan\n\n| # | Channel | Time | What happens |\n|---|---|---|---|\n",
    ],
    ids=["missing_section", "missing_table", "empty_table"],
)
def test_parse_morning_plan_raises(text):
    with pytest.raises(MorningPlanError):
        parse_morning_plan(text, README_URL)


def test_load_hn_snapshots_present_and_missing(tmp_path):
    day = date(2026, 9, 26)
    (tmp_path / f"hn-jobs-{day.isoformat()}-freelance.txt").write_text("freelance data")

    snapshots = load_hn_snapshots(tmp_path, day)

    assert snapshots == [
        {
            "kind": "freelance",
            "filename": "hn-jobs-2026-09-26-freelance.txt",
            "present": True,
            "text": "freelance data",
        },
        {
            "kind": "hiring",
            "filename": "hn-jobs-2026-09-26-hiring.txt",
            "present": False,
            "text": None,
        },
    ]


def _configure(tmp_path, monkeypatch, section=None, readme_text=SYNTHETIC_README):
    """A plan README, a snapshots dir and a config naming them (relative paths, as documented)."""
    (tmp_path / "plans" / "snapshots").mkdir(parents=True)
    (tmp_path / "plans" / "README.md").write_text(readme_text)
    if section is None:
        section = f'readme = "plans/README.md"\nlink_base = "{LINK_BASE}/"\nsnapshots = "plans/snapshots"\n'
    (tmp_path / "config.toml").write_text(f"[morning]\n{section}")
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))


def test_morning_endpoint_returns_steps_from_the_configured_readme(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    response = TestClient(app).get("/api/morning")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["date"]) == 10
    assert payload["date"][4] == "-"
    assert [step["channel"] for step in payload["steps"]] == [
        "Replies",
        "Hacker News",
        "Upwork",
    ]
    assert payload["source"] == str((tmp_path / "plans" / "README.md").resolve())
    assert payload["intro"][1][1]["href"] == f"{LINK_BASE}/applications/TRACKER.md"
    assert [snap["kind"] for snap in payload["snapshots"]] == ["freelance", "hiring"]
    assert all(snap["present"] is False for snap in payload["snapshots"])
    assert all(snap["text"] is None for snap in payload["snapshots"])
    assert all(snap["filename"].startswith("hn-jobs-") for snap in payload["snapshots"])


@pytest.mark.parametrize("section, detail", [
    ('readme = "nowhere.md"\nlink_base = "https://x.test"\n', "Morning plan source not found"),
    ('readme = "plans/README.md"\n', "[morning] readme and link_base are required"),
    ('readme = "plans/README.md"\nlink_base = "https://x.test"\nextra = "x"\n', "[morning] may only set"),
])
def test_morning_endpoint_503_names_the_problem(tmp_path, monkeypatch, section, detail):
    _configure(tmp_path, monkeypatch, section)

    response = TestClient(app).get("/api/morning")

    assert response.status_code == 503
    assert detail in response.json()["detail"]


def test_morning_endpoint_503_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "absent.toml"))

    response = TestClient(app).get("/api/morning")

    assert response.status_code == 503
    assert response.json()["detail"].startswith("the Morning page is not configured")


def test_no_snapshots_key_means_no_snapshots(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, f'readme = "plans/README.md"\nlink_base = "{LINK_BASE}"\n')

    assert morning_snapshot(date(2026, 9, 26))["snapshots"] == []


def test_morning_snapshot_uses_local_date(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    snapshot = morning_snapshot(date(2026, 9, 26))

    assert snapshot["date"] == "2026-09-26"
    assert snapshot["steps"]


def test_fragment_only_link_points_at_the_readme():
    plan = parse_morning_plan(
        "## Standing morning plan\n\n"
        "| # | Channel | Time | What happens |\n|---|---|---|---|\n"
        "| 0 | Replies | 2 min | See [the rules](#the-rules). |\n",
        README_URL,
    )
    link = next(s for s in plan["steps"][0]["description"] if s["type"] == "link")
    assert link["href"] == f"{LINK_BASE}/README.md#the-rules"


def test_link_target_with_parentheses_is_kept_whole():
    plan = parse_morning_plan(
        "## Standing morning plan\n\n"
        "| # | Channel | Time | What happens |\n|---|---|---|---|\n"
        "| 0 | Read | 2 min | See [Foo](https://en.wikipedia.org/wiki/Foo_(bar)) now. |\n",
        README_URL,
    )
    description = plan["steps"][0]["description"]
    link = next(s for s in description if s["type"] == "link")
    assert link["href"] == "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert description[-1] == {"type": "text", "text": " now."}


def test_unreadable_snapshot_is_present_but_textless_and_logged(tmp_path, monkeypatch, caplog):
    day = date(2026, 9, 26)
    posting = tmp_path / "hn-jobs-2026-09-26-freelance.txt"
    posting.write_text("posting")
    posting.chmod(0)
    try:
        with caplog.at_level("ERROR"):
            snapshots = load_hn_snapshots(tmp_path, day)
    finally:
        posting.chmod(0o600)

    freelance = next(s for s in snapshots if s["kind"] == "freelance")
    assert freelance["present"] is True
    assert freelance["text"] is None
    assert "Could not read HN snapshot" in caplog.text


def _doctor_ok():
    import json

    import pt
    from click.testing import CliRunner

    doctor = json.loads(CliRunner().invoke(pt.cli, ["doctor", "--json"]).output)
    return next(c for c in doctor["checks"] if c["name"] == "local_config")["ok"]


# For each path setting: the value, the endpoint status and the text the
# response shows. The config only says where the files are, so doctor calls it
# healthy in every case; the page reads them and names whatever the OS reports.
# Only a missing file is "not found"; every other failure is "could not be read".
READ = "Morning plan source could not be read"
PATH_CASES = {
    ("readme", "valid"): ("plans/README.md", 200, "Replies"),
    ("readme", "missing"): ("plans/absent.md", 503, "Morning plan source not found"),
    ("readme", "no_access"): ("locked/README.md", 503, READ),
    ("readme", "too_long"): ("plans/" + "x" * 300, 503, READ),
    # The OS meets the missing directory first, so this one is "not found".
    ("readme", "missing_then_long"): ("plans/absent/../" + "x" * 300, 503, "Morning plan source not found"),
    ("readme", "a_directory"): ("plans", 503, READ),
    ("readme", "trailing_slash"): ("plans/README.md/", 503, READ),
    ("readme", "trailing_dot"): ("plans/README.md/.", 503, READ),
    ("readme", "unreadable"): ("plans/README.md", 503, READ),
    ("readme", "not_utf8"): ("plans/README.md", 503, READ),
    ("snapshots", "valid"): ("plans/snapshots", 200, "freelance data"),
    ("snapshots", "valid_dot"): ("plans/snapshots/.", 200, "freelance data"),
    ("snapshots", "missing"): ("plans/absent", 200, '"present":false'),
    ("snapshots", "no_access"): ("locked/snapshots", 200, '"present":true,"text":null'),
    ("snapshots", "too_long"): ("plans/" + "x" * 300, 200, '"present":true,"text":null'),
    ("snapshots", "missing_then_long"): ("plans/absent/../" + "x" * 300, 200, '"present":false'),
    ("snapshots", "a_file"): ("plans/README.md", 200, '"present":true,"text":null'),
    ("snapshots", "unreadable"): ("plans/snapshots", 200, '"present":true,"text":null'),
    ("snapshots", "not_utf8"): ("plans/snapshots", 200, '"present":true,"text":null'),
}


@pytest.mark.parametrize("key, case", PATH_CASES)
def test_each_path_failure_is_named_by_the_page_and_the_config_stays_valid(tmp_path, monkeypatch, key, case):
    value, status, shown = PATH_CASES[key, case]
    paths = {"readme": "plans/README.md", "snapshots": "plans/snapshots", key: value}
    _configure(tmp_path, monkeypatch, f'readme = "{paths["readme"]}"\nlink_base = "{LINK_BASE}"\n'
                                      f'snapshots = "{paths["snapshots"]}"\n')
    snapshot = tmp_path / "plans" / "snapshots" / f"hn-jobs-{date.today().isoformat()}-freelance.txt"
    snapshot.write_text("freelance data")
    locked = tmp_path / "locked"
    (locked / "snapshots").mkdir(parents=True)
    (locked / "README.md").write_text(SYNTHETIC_README)
    target = {"readme": tmp_path / "plans" / "README.md", "snapshots": snapshot}[key]
    locked.chmod(0)
    if case == "unreadable":
        target.chmod(0)
    if case == "not_utf8":
        target.write_bytes(b"\xff\xfe")
    try:
        response = TestClient(app).get("/api/morning")
        assert (response.status_code, True) == (status, _doctor_ok()), response.text
        assert shown in response.text
    finally:
        locked.chmod(0o700)
        target.chmod(0o600)


@pytest.mark.parametrize("name, encoded", [
    ("README.md", "README.md"), ("plan#draft.md", "plan%23draft.md"),
    ("what?.md", "what%3F.md"), ("100%.md", "100%25.md"), ("my plan.md", "my%20plan.md"),
])
def test_fragment_links_point_at_the_configured_file_whatever_its_name(tmp_path, monkeypatch, name, encoded):
    text = ("## Standing morning plan\n\n| # | Channel | Time | What happens |\n|---|---|---|---|\n"
            "| 0 | Replies | 2 min | See [the rules](#rules) and [a file](notes.md). |\n")
    _configure(tmp_path, monkeypatch, f'readme = "plans/{name}"\nlink_base = "{LINK_BASE}"\n')
    (tmp_path / "plans" / name).write_text(text)
    links = [seg["href"] for seg in morning_snapshot(date(2026, 9, 26))["steps"][0]["description"]
             if seg["type"] == "link"]
    assert links == [f"{LINK_BASE}/{encoded}#rules", f"{LINK_BASE}/notes.md"]
