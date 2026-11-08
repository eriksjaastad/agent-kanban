"""Tests for the dashboard's Kanban breakdown (#7181).

The dashboard's top section reports how many open cards each project is
carrying in each board column. Two things have to hold for that section to be
worth trusting: the counts must be the board's counts (no Done, no Cancelled,
no archived rows), and each column must arrive ranked so the biggest pile is
first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app
from db.manager import DatabaseManager
from db.schema import create_database


@pytest.fixture
def owner_robin(tmp_path):
    """The test's own local config names the board's owner Robin (cards start "ROBIN:")."""
    (tmp_path / "_local" / "config.toml").write_text('[owner]\nname = "Robin"\n', encoding="utf-8")


def _setup_db(tmp_path: Path) -> tuple[Path, DatabaseManager]:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id="project-tracker",
        name="Project Tracker",
        path=str(tmp_path / "project-tracker"),
        status="active",
    )
    db.add_project(
        project_id="holoapp",
        name="holoapp",
        path=str(tmp_path / "holoapp"),
        status="active",
    )
    return db_path, db


def _breakdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_path: Path) -> dict:
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    response = TestClient(dashboard_app.app).get("/api/kanban/breakdown")
    assert response.status_code == 200, response.text
    return response.json()


def test_breakdown_counts_each_project_per_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)

    for _ in range(3):
        db.add_task("backlog card", "holoapp", status="Backlog")
    db.add_task("backlog card", "project-tracker", status="Backlog")
    db.add_task("todo card", "project-tracker", status="To Do")
    db.add_task("wip card", "holoapp", status="In Progress")

    payload = _breakdown(tmp_path, monkeypatch, db_path)

    assert payload["statuses"] == ["Backlog", "To Do", "In Progress", "Review"]
    assert payload["totals"] == {"Backlog": 4, "To Do": 1, "In Progress": 1, "Review": 0}
    assert payload["columns"]["Backlog"] == [
        {"project_id": "holoapp", "project": "holoapp", "count": 3},
        {"project_id": "project-tracker", "project": "Project Tracker", "count": 1},
    ]
    assert payload["columns"]["Review"] == []


def test_breakdown_ranks_projects_by_card_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)

    # Insert the small pile first: ranking must come from the counts, not from
    # insertion order or SQLite's grouping order.
    db.add_task("only card", "project-tracker", status="Backlog")
    for _ in range(5):
        db.add_task("backlog card", "holoapp", status="Backlog")

    payload = _breakdown(tmp_path, monkeypatch, db_path)

    counts = [entry["count"] for entry in payload["columns"]["Backlog"]]
    assert counts == [5, 1]


def test_breakdown_excludes_closed_and_archived_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)

    db.add_task("open card", "holoapp", status="Backlog")
    db.add_task("finished card", "holoapp", status="Done")
    db.add_task("dropped card", "holoapp", status="Cancelled")

    payload = _breakdown(tmp_path, monkeypatch, db_path)

    assert payload["totals"] == {"Backlog": 1, "To Do": 0, "In Progress": 0, "Review": 0}
    assert set(payload["columns"]) == {"Backlog", "To Do", "In Progress", "Review"}


def test_counts_drop_archived_rows_unless_asked(tmp_path: Path):
    """Archived Done cards still exist; they just are not on the board."""
    _, db = _setup_db(tmp_path)

    db.add_task("finished card", "holoapp", status="Done")
    assert db.archive_done_tasks(keep_per_project=0) == 1

    visible = db.get_task_counts_by_project(statuses=["Done"])
    archived = db.get_task_counts_by_project(statuses=["Done"], include_archived=True)

    assert visible == []
    assert archived == [{"project_id": "holoapp", "status": "Done", "count": 1}]


def test_waiting_on_owner_lists_open_owner_cards_oldest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             owner_robin):
    db_path, db = _setup_db(tmp_path)
    newer = db.add_task("ROBIN: newer ask", "holoapp", status="To Do")["id"]
    older = db.add_task("ROBIN: older ask", "project-tracker", status="Backlog")["id"]
    db.add_task("ROBIN: answered", "holoapp", status="Done")
    db.add_task("ROBIN: an idea", "holoapp", status="Backlog", task_type="proposal")
    db.add_task("robin: not the prefix", "holoapp", status="Backlog")
    db.add_task("ordinary card mentioning ROBIN:", "holoapp", status="Backlog")
    with db._get_conn() as conn:
        conn.execute("UPDATE tasks SET created_at = '2026-01-01T09:00:00' WHERE id = ?", (older,))
        conn.commit()

    data = _breakdown(tmp_path, monkeypatch, db_path)
    waiting = data["waiting_on_owner"]
    assert (data["owner"], data["owner_prefix"]) == ("Robin", "ROBIN:")

    assert [card["text"] for card in waiting] == ["ROBIN: older ask", "ROBIN: newer ask"]
    assert waiting[0]["project"] == "Project Tracker"
    assert waiting[0]["display_id"] == db.get_task_display_id(older)
    assert waiting[0]["age_days"] > 300 and waiting[1]["age_days"] == 0
    assert db.get_task_display_id(newer) == waiting[1]["display_id"]


def test_weekly_split_follows_the_editable_user_facing_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from datetime import date, timedelta

    db_path, db = _setup_db(tmp_path)
    monday = date.today() - timedelta(days=date.today().weekday())
    done_on = {
        ("holoapp", "Done"): monday,
        ("project-tracker", "Done"): monday,
        ("holoapp", "Done", 3): monday - timedelta(weeks=3),
        ("holoapp", "Done", 9): monday - timedelta(weeks=9),   # outside the 8 weeks
        ("holoapp", "Cancelled"): monday,
    }
    cards = {db.add_task("card", key[0], status=key[1])["id"]: day for key, day in done_on.items()}
    with db._get_conn() as conn:
        for card, day in cards.items():
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (f"{day}T10:00:00", card))
        conn.commit()
    config = tmp_path / "config.toml"
    monkeypatch.setenv("PT_CONFIG", str(config))
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    asked = []
    monkeypatch.setattr(dashboard_app, "merged_prs_weekly", lambda uf, starts: asked.append(uf) or {"weeks": []})
    client = TestClient(dashboard_app.app)

    config.write_text('[dashboard]\nuser_facing = ["holoapp"]\n')
    weeks = client.get("/api/stats/user-facing-weekly").json()["weeks"]
    assert [w["week_start"] for w in weeks] == [str(monday - timedelta(weeks=n)) for n in range(7, -1, -1)]
    assert weeks[-1] == {"week_start": str(monday), "user_facing": 1, "internal": 1}
    assert weeks[-4] == {"week_start": str(monday - timedelta(weeks=3)), "user_facing": 1, "internal": 0}
    assert sum(w["user_facing"] + w["internal"] for w in weeks) == 3

    config.write_text('[dashboard]\nuser_facing = []\n')
    assert client.get("/api/stats/user-facing-weekly").json()["weeks"][-1]["internal"] == 2
    assert asked == [frozenset({"holoapp"}), frozenset()]

    config.write_text('[dashboard]\nuser_facing = "holoapp"\n')
    response = client.get("/api/stats/user-facing-weekly")
    assert response.status_code == 500 and "user_facing must be a list" in response.json()["detail"]


def test_an_unnamed_owner_is_owner_and_no_name_is_built_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """#8961: with no [owner] in the local config, the board waits on "Owner"."""
    db_path, db = _setup_db(tmp_path)
    db.add_task("OWNER: pick a name", "holoapp", status="Backlog")
    db.add_task("ROBIN: someone else's ask", "holoapp", status="Backlog")

    data = _breakdown(tmp_path, monkeypatch, db_path)

    assert (data["owner"], data["owner_prefix"]) == ("Owner", "OWNER:")
    assert [card["text"] for card in data["waiting_on_owner"]] == ["OWNER: pick a name"]
