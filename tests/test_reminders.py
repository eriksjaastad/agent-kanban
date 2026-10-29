"""Reminders: manager mixin, dashboard API and `pt reminders` (#8282)."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from pt import cli  # noqa: E402
from dashboard.app import app  # noqa: E402

client = TestClient(app)
ENV = {"PT_NO_BANNER": "1", "PT_SUPPRESS_MIGRATION_WARNING": "1"}


def _pt(*args: str):
    return CliRunner().invoke(cli, list(args), env=ENV)


def _add(name: str, **fields) -> dict:
    response = client.post("/api/reminders", json={"name": name, **fields})
    assert response.status_code == 201, response.text
    return response.json()["reminder"]


def _names(section: str) -> list[str]:
    return [r["name"] for r in client.get("/api/reminders").json()[section]]


def test_reminder_lifecycle_add_built_in_remove() -> None:
    first = _add("  Prompt pack  ", purpose="Run an audit", location="~/x")
    assert (first["name"], first["purpose"], first["location"]) == ("Prompt pack", "Run an audit", "~/x")
    assert _add("Guide", purpose="  ", location="")["purpose"] is None
    third = _add("Roadmap")
    assert client.post("/api/reminders", json={"name": "   "}).status_code == 400
    # Active reminders keep the order they were added in.
    assert _names("active") == ["Prompt pack", "Guide", "Roadmap"]

    built = client.post(f"/api/reminders/{first['id']}/built-in")
    assert built.status_code == 200 and built.json()["reminder"]["built_in_at"]
    assert _names("active") == ["Guide", "Roadmap"]
    assert _names("built_in") == ["Prompt pack"]

    assert client.delete(f"/api/reminders/{third['id']}").status_code == 200
    assert _names("active") == ["Guide"]
    # A built-in reminder can still be removed.
    assert client.delete(f"/api/reminders/{first['id']}").status_code == 200
    assert _names("built_in") == []
    # A removed reminder is gone for both actions; the row itself is kept.
    assert client.delete(f"/api/reminders/{third['id']}").status_code == 404
    assert client.post(f"/api/reminders/{third['id']}/built-in").status_code == 404
    assert client.delete("/api/reminders/999999").status_code == 404


def test_built_in_mark_loses_to_a_removal_that_lands_first(db, monkeypatch) -> None:
    reminder = db.add_reminder("Guide")
    original = type(db)._get_conn

    class RemovalLandsFirst:
        """Lets another connection remove the row just before the built-in UPDATE."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE reminders SET built_in_at"):
                with sqlite3.connect(os.environ["PT_DB_PATH"]) as other:
                    other.execute(
                        "UPDATE reminders SET deleted_at = 'now' WHERE id = ?", (reminder["id"],))
            return self._conn.execute(sql, *args)

    @contextmanager
    def racing_conn(self):
        with original(self) as conn:
            yield RemovalLandsFirst(conn)

    monkeypatch.setattr(type(db), "_get_conn", racing_conn)
    assert db.mark_reminder_built_in(reminder["id"]) is None
    monkeypatch.undo()
    assert db.list_reminders() == {"active": [], "built_in": []}


def test_pt_reminders_add_appears_on_the_page_and_in_the_list() -> None:
    result = _pt("reminders", "add", "Prompt pack", "--purpose", "Audit", "--location", "~/p")
    assert result.exit_code == 0, result.output
    [reminder] = client.get("/api/reminders").json()["active"]
    assert (reminder["name"], reminder["purpose"], reminder["location"]) == (
        "Prompt pack", "Audit", "~/p")

    listed = _pt("reminders")
    assert listed.exit_code == 0, listed.output
    assert f"#{reminder['id']} Prompt pack" in listed.output


def test_pt_reminders_built_in_retires_it_from_the_page() -> None:
    reminder = _add("Cold email guide")
    result = _pt("reminders", "built-in", str(reminder["id"]))
    assert result.exit_code == 0, result.output
    assert _names("active") == [] and _names("built_in") == ["Cold email guide"]
    assert _pt("reminders", "built-in", "999999").exit_code != 0


def test_pt_reminders_without_migration_022_says_to_migrate() -> None:
    # The per-test database copy, not the live one.
    with sqlite3.connect(os.environ["PT_DB_PATH"]) as conn:
        conn.execute("DROP TABLE reminders")
    result = _pt("reminders")
    assert result.exit_code != 0
    assert "run `pt db migrate`" in result.output
