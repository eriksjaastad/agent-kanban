"""Tests for the `pt skills stats` CLI (#5913)."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from skills_registry import installed_skills
from skill_invocations_reader import (
    SkillInvocationsTableMissing,
    iter_invocations,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_invocations (
    id INTEGER PRIMARY KEY,
    skill_name TEXT NOT NULL,
    cwd TEXT,
    session_id TEXT,
    tool_use_id TEXT,
    caller_type TEXT,
    invoked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    next_errors INTEGER,
    enriched_at TIMESTAMP,
    UNIQUE(session_id, tool_use_id)
);
CREATE INDEX IF NOT EXISTS idx_skill_name ON skill_invocations(skill_name);
CREATE INDEX IF NOT EXISTS idx_invoked_at ON skill_invocations(invoked_at DESC);
"""


def _seed(db_path: Path, rows: list[tuple]) -> None:
    """Create schema and insert rows (skill_name, cwd, session, tool_id, caller, invoked_at)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO skill_invocations (skill_name, cwd, session_id, tool_use_id, caller_type, invoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Point HOME at a temp dir with a projects/ layout the origin helper expects."""
    home = tmp_path / "home"
    (home / "projects" / "project-tracker").mkdir(parents=True)
    (home / "projects" / "ai-metrics" / "hooks").mkdir(parents=True)
    (home / "projects" / "other").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PT_CALLER_CWD", raising=False)
    return home


def _skills_fixture(home: Path, names: list[str]) -> Path:
    """Drop SKILL.md files under ``~/.claude/skills/<slug>/SKILL.md``."""
    root = home / ".claude" / "skills"
    for name in names:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\nBody\n",
            encoding="utf-8",
        )
    return root


# ── skills_registry ───────────────────────────────────────────────────────


def test_installed_skills_parses_frontmatter(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    root = _skills_fixture(home, ["commit", "pr", "journal"])
    out = installed_skills(root)
    assert set(out.keys()) == {"commit", "pr", "journal"}
    assert all(p.name == "SKILL.md" for p in out.values())


def test_installed_skills_ignores_missing_frontmatter(tmp_path, monkeypatch, capsys):
    home = _fake_home(tmp_path, monkeypatch)
    root = home / ".claude" / "skills"
    good = root / "good"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text("---\nname: good\n---\n", encoding="utf-8")
    bad = root / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter at all\n", encoding="utf-8")
    out = installed_skills(root)
    assert set(out.keys()) == {"good"}
    captured = capsys.readouterr()
    assert "skipping" in captured.err
    assert "bad/SKILL.md" in captured.err


def test_installed_skills_handles_utf8_bom(tmp_path, monkeypatch, capsys):
    """UTF-8 BOM-prefixed SKILL.md files should still parse (regex allows BOM)."""
    home = _fake_home(tmp_path, monkeypatch)
    root = home / ".claude" / "skills"
    d = root / "bom-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(
        "\ufeff---\nname: bom-skill\n---\nBody\n".encode("utf-8")
    )
    out = installed_skills(root)
    assert "bom-skill" in out
    assert "skipping" not in capsys.readouterr().err


def test_installed_skills_empty_on_missing_root(tmp_path):
    assert installed_skills(tmp_path / "nope") == {}


def test_installed_skills_finds_nested_SKILL_md(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    root = home / ".claude" / "skills"
    nested = root / "apple-product" / "idea-generator"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: idea-generator\n---\n", encoding="utf-8"
    )
    out = installed_skills(root)
    assert "idea-generator" in out


# ── skill_invocations_reader: timestamp parsing ───────────────────────────


@pytest.mark.parametrize(
    "skill, project, raw, expected",
    [
        # `CURRENT_TIMESTAMP` default writes 'YYYY-MM-DD HH:MM:SS' without tz;
        # the parser must normalise that to UTC-aware.
        ("commit", "project-tracker", "2026-04-10 12:34:56", "2026-04-10T12:34:56+00:00"),
        # Some older rows carry trailing 'Z' instead of '+00:00'.
        ("pr", "ai-metrics", "2026-03-16T08:40:50.204Z", "2026-03-16T08:40:50.204000+00:00"),
    ],
    ids=["naive_sqlite_default", "z_suffix"],
)
def test_reader_parses_timestamp_to_utc_aware(
    tmp_path, monkeypatch, skill, project, raw, expected
):
    home = _fake_home(tmp_path, monkeypatch)
    db = tmp_path / "brain.db"
    _seed(db, [
        (skill, str(home / "projects" / project), "s1", "t1", "", raw),
    ])
    rows = list(iter_invocations(db_path=db))
    assert len(rows) == 1
    assert rows[0].invoked_at.tzinfo is not None
    assert rows[0].invoked_at.isoformat() == expected


# ── skill_invocations_reader ──────────────────────────────────────────────


def test_reader_raises_when_table_missing(tmp_path, monkeypatch):
    _fake_home(tmp_path, monkeypatch)
    db = tmp_path / "brain.db"
    sqlite3.connect(db).close()
    with pytest.raises(SkillInvocationsTableMissing):
        list(iter_invocations(db_path=db))


def test_reader_filters_by_skill_and_since(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    db = tmp_path / "brain.db"
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    _seed(db, [
        ("commit", str(home / "projects" / "project-tracker"), "s1", "t1", "", recent),
        ("commit", str(home / "projects" / "project-tracker"), "s1", "t2", "", old),
        ("pr",     str(home / "projects" / "ai-metrics"),       "s2", "t3", "", recent),
    ])

    rows = list(iter_invocations(db_path=db))
    assert len(rows) == 3

    recent_only = list(iter_invocations(
        since=datetime.now(timezone.utc) - timedelta(days=7),
        db_path=db,
    ))
    assert len(recent_only) == 2

    commit_only = list(iter_invocations(skill="commit", db_path=db))
    assert [r.skill_name for r in commit_only] == ["commit", "commit"]


def test_reader_filters_by_agent(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    db = tmp_path / "brain.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _seed(db, [
        ("commit", str(home / "projects" / "project-tracker"), "s1", "t1", "main", now),
        ("pr",     str(home / "projects" / "project-tracker"), "s1", "t2", "main", now),
        ("pr",     str(home / "projects" / "ai-metrics"),       "s2", "t3", "subagent", now),
        ("journal", str(home / "projects" / "other"),          "s3", "t4", "", now),
    ])

    main_rows = list(iter_invocations(agent="main", db_path=db))
    assert sorted(r.skill_name for r in main_rows) == ["commit", "pr"]
    assert all(r.caller_type == "main" for r in main_rows)

    sub_rows = list(iter_invocations(agent="subagent", db_path=db))
    assert len(sub_rows) == 1 and sub_rows[0].caller_type == "subagent"

    empty_rows = list(iter_invocations(agent="", db_path=db))
    assert len(empty_rows) == 1 and empty_rows[0].skill_name == "journal"


def test_reader_resolves_project_from_cwd(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, monkeypatch)
    db = tmp_path / "brain.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _seed(db, [
        ("commit", str(home / "projects" / "project-tracker"), "s1", "t1", "", now),
        ("commit", str(home / "projects" / "ai-metrics" / "hooks"), "s2", "t2", "", now),
        ("pr",     str(home / "projects"), "s3", "t3", "", now),
        ("pr",     "/tmp/elsewhere", "s4", "t4", "", now),
    ])

    rows = list(iter_invocations(db_path=db))
    projects = sorted(r.project for r in rows)
    assert projects == ["ai-metrics", "architect", "project-tracker", "unknown"]

    tracker_rows = list(iter_invocations(
        project="project-tracker", db_path=db,
    ))
    assert len(tracker_rows) == 1
    assert tracker_rows[0].project == "project-tracker"


# ── CLI surface ───────────────────────────────────────────────────────────


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Seed a temp home + brain.db, return (home, db)."""
    home = _fake_home(tmp_path, monkeypatch)
    db = tmp_path / "brain.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    _seed(db, [
        ("commit", str(home / "projects" / "project-tracker"), "s1", "t1", "main", now),
        ("commit", str(home / "projects" / "project-tracker"), "s1", "t2", "main", now),
        ("pr",     str(home / "projects" / "ai-metrics"),       "s2", "t3", "subagent", now),
        ("journal", str(home / "projects" / "other"),          "s3", "t4", "", old),
    ])

    # Point the reader at our fake paths via monkeypatching the module defaults.
    import skill_invocations_reader as reader_mod
    monkeypatch.setattr(reader_mod, "DEFAULT_BRAIN_DB", db)

    # Seed installed skills via the frontmatter walker.
    _skills_fixture(home, ["commit", "pr", "journal", "never-called"])

    return home, db


def _runner_invoke(args: list[str]):
    """Import pt.py lazily so cli_env monkeypatching is in place first."""
    from pt import skills_group
    return CliRunner().invoke(skills_group, args)


def test_cli_default_leaderboard(cli_env):
    result = _runner_invoke(["stats", "--days", "0"])
    assert result.exit_code == 0, result.output
    assert "commit" in result.output
    assert "journal" in result.output


def test_cli_days_window_filters_old_rows(cli_env):
    result = _runner_invoke(["stats", "--days", "7", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    skills = {s["skill"]: s["calls"] for s in payload["skills"]}
    assert skills == {"commit": 2, "pr": 1}
    assert "journal" not in skills


def test_cli_project_filter(cli_env):
    result = _runner_invoke(["stats", "--days", "0", "--project", "project-tracker", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    skills = {s["skill"]: s["calls"] for s in payload["skills"]}
    assert skills == {"commit": 2}


def test_cli_skill_filter(cli_env):
    result = _runner_invoke(["stats", "--days", "0", "--skill", "pr", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["skills"]) == 1
    assert payload["skills"][0]["skill"] == "pr"


def test_cli_never_used(cli_env):
    result = _runner_invoke(["stats", "--days", "0", "--never-used", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "never-called" in payload["never_used"]
    assert "commit" not in payload["never_used"]


def test_cli_by_agent(cli_env):
    result = _runner_invoke(["stats", "--days", "0", "--by-agent", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    agents = payload["agents"]
    assert agents["main"] == {"commit": 2}
    assert agents["subagent"] == {"pr": 1}
    assert agents["unknown"] == {"journal": 1}


@pytest.mark.parametrize(
    "agent, expected",
    [
        ("main", {"commit": 2}),
        # --agent unknown translates to caller_type='' for the SQL query.
        ("unknown", {"journal": 1}),
        ("subagent", {"pr": 1}),
    ],
)
def test_cli_agent_filter(cli_env, agent, expected):
    result = _runner_invoke(["stats", "--days", "0", "--agent", agent, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    skills = {s["skill"]: s["calls"] for s in payload["skills"]}
    assert skills == expected


def test_cli_render_modes_are_mutually_exclusive(cli_env):
    """--never-used, --by-project, and --by-agent cannot be combined."""
    for pair in (["--by-project", "--by-agent"],
                 ["--never-used", "--by-agent"],
                 ["--never-used", "--by-project"]):
        result = _runner_invoke(["stats", "--days", "0", *pair])
        assert result.exit_code != 0, f"expected UsageError for {pair}: {result.output}"
        assert "mutually exclusive" in result.output


def test_cli_by_project(cli_env):
    result = _runner_invoke(["stats", "--days", "0", "--by-project", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    projects = payload["projects"]
    assert projects["project-tracker"] == {"commit": 2}
    assert projects["ai-metrics"] == {"pr": 1}
    assert projects["other"] == {"journal": 1}
