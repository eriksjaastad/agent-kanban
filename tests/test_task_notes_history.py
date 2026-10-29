"""`pt tasks update --notes` must not silently wipe notes (#7642).

``pt tasks update --notes`` used to do a full replace of the notes column.
That column also holds acceptance criteria from ``pt tasks create -d``, so an
agent adding "a note" erased a card's brief. There was no history and no
refusal. These tests pin the new contract:

- ``--append-notes`` never removes text and stamps the entry.
- ``--notes`` refuses to overwrite non-empty notes without ``--replace-notes``.
- Every real notes change writes one ``task_notes_history`` row with the
  right source, and identical saves write none.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402
from pt import tasks_group  # noqa: E402

PROJECT_ID = "project-tracker"


@pytest.fixture
def backend_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    """Database manager that the in-process CLI and dashboard can reach."""
    db_path = tmp_path / "test.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    manager = DatabaseManager()
    manager.add_project(
        project_id=PROJECT_ID,
        name="Project Tracker",
        path="/tmp/project-tracker",
        status="active",
    )
    return manager


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _history(db: DatabaseManager, task_id: int) -> list[dict]:
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id, task_id, project_id, old_notes, new_notes, source, timestamp "
            "FROM task_notes_history WHERE task_id = ? "
            "ORDER BY timestamp DESC, id DESC",
            (task_id,),
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------
# update --append-notes
# ---------------------------------------------------------------------


def test_append_notes_preserves_existing_text_and_stamps(backend_db, runner):
    task = backend_db.add_task(
        text="Append target", project_id=PROJECT_ID, notes="brief line 1\nbrief line 2"
    )
    display_id = backend_db.get_task_display_id(task["id"])

    result = runner.invoke(
        tasks_group, ["update", str(display_id), "--append-notes", "added note"]
    )

    assert result.exit_code == 0, result.output
    stored = backend_db.get_task(task["id"])
    assert stored["notes"].startswith("brief line 1\nbrief line 2\n\n[")
    assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] added note", stored["notes"])
    rows = _history(backend_db, task["id"])
    assert len(rows) == 1
    assert rows[0]["old_notes"] == "brief line 1\nbrief line 2"
    assert rows[0]["new_notes"] == stored["notes"]
    assert rows[0]["source"] == "cli"


def test_append_notes_on_empty_works(backend_db, runner):
    task = backend_db.add_task(text="Empty notes", project_id=PROJECT_ID)

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(task["id"])),
         "--append-notes", "first note"],
    )

    assert result.exit_code == 0, result.output
    stored = backend_db.get_task(task["id"])
    assert re.fullmatch(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] first note", stored["notes"]
    )
    rows = _history(backend_db, task["id"])
    assert len(rows) == 1
    assert rows[0]["old_notes"] is None
    assert rows[0]["new_notes"] == stored["notes"]
    assert rows[0]["source"] == "cli"


@pytest.mark.parametrize(
    ("flag", "text", "stored_exactly"),
    [
        # append stamps a timestamp, so the text is only contained in the notes
        ("--append-notes", "see [/] and - [x] ok", False),
        # --notes on an empty card stores the text as given
        ("--notes", "[/]", True),
    ],
)
def test_notes_markup_like_text_exits_zero_and_echoes_verbatim(
    backend_db, runner, flag, text, stored_exactly
):
    task = backend_db.add_task(text="Markup", project_id=PROJECT_ID)

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(task["id"])), flag, text],
    )

    assert result.exit_code == 0, result.output
    stored = backend_db.get_task(task["id"])
    if stored_exactly:
        assert stored["notes"] == text
    else:
        assert text in stored["notes"]
    assert text in result.output


# ---------------------------------------------------------------------
# update --notes (replace)
# ---------------------------------------------------------------------


def test_notes_refuses_to_replace_nonempty_without_flag(backend_db, runner):
    task = backend_db.add_task(
        text="Keep brief", project_id=PROJECT_ID, notes="original brief"
    )

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(task["id"])),
         "--notes", "just a note"],
    )

    assert result.exit_code != 0
    assert "--append-notes" in result.output
    assert "--replace-notes" in result.output
    assert backend_db.get_task(task["id"])["notes"] == "original brief"
    assert _history(backend_db, task["id"]) == []


def test_notes_with_replace_flag_writes_one_history_row(backend_db, runner):
    task = backend_db.add_task(
        text="Replace me", project_id=PROJECT_ID, notes="old brief"
    )

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(task["id"])),
         "--notes", "new brief", "--replace-notes"],
    )

    assert result.exit_code == 0, result.output
    assert backend_db.get_task(task["id"])["notes"] == "new brief"
    rows = _history(backend_db, task["id"])
    assert len(rows) == 1
    assert rows[0]["old_notes"] == "old brief"
    assert rows[0]["new_notes"] == "new brief"
    assert rows[0]["source"] == "cli"


def test_notes_on_empty_works_without_flag(backend_db, runner):
    task = backend_db.add_task(text="Empty", project_id=PROJECT_ID)

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(task["id"])),
         "--notes", "first brief"],
    )

    assert result.exit_code == 0, result.output
    assert backend_db.get_task(task["id"])["notes"] == "first brief"
    rows = _history(backend_db, task["id"])
    assert len(rows) == 1
    assert rows[0]["old_notes"] is None
    assert rows[0]["source"] == "cli"


def test_notes_and_append_notes_together_is_error(backend_db, runner):
    task = backend_db.add_task(text="Conflict", project_id=PROJECT_ID)

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(task["id"])),
         "--notes", "x", "--append-notes", "y"],
    )

    assert result.exit_code != 0
    assert backend_db.get_task(task["id"])["notes"] is None
    assert _history(backend_db, task["id"]) == []


def test_replace_notes_without_notes_is_error(backend_db, runner):
    task = backend_db.add_task(text="Flag only", project_id=PROJECT_ID)

    result = runner.invoke(
        tasks_group,
        ["update", str(backend_db.get_task_display_id(task["id"])),
         "--replace-notes"],
    )

    assert result.exit_code != 0


# ---------------------------------------------------------------------
# dashboard PATCH /api/tasks/{id}
# ---------------------------------------------------------------------


def test_dashboard_patch_notes_writes_api_history(backend_db):
    task = backend_db.add_task(
        text="API notes", project_id=PROJECT_ID, notes="old api notes"
    )

    response = TestClient(dashboard_app.app).patch(
        f"/api/tasks/{task['id']}", json={"notes": "new api notes"}
    )

    assert response.status_code == 200, response.text
    assert backend_db.get_task(task["id"])["notes"] == "new api notes"
    rows = _history(backend_db, task["id"])
    assert len(rows) == 1
    assert rows[0]["old_notes"] == "old api notes"
    assert rows[0]["new_notes"] == "new api notes"
    assert rows[0]["source"] == "api"


def test_dashboard_patch_identical_notes_writes_no_history(backend_db):
    task = backend_db.add_task(
        text="API same", project_id=PROJECT_ID, notes="same notes"
    )

    response = TestClient(dashboard_app.app).patch(
        f"/api/tasks/{task['id']}", json={"notes": "same notes"}
    )

    assert response.status_code == 200, response.text
    assert _history(backend_db, task["id"]) == []


# ---------------------------------------------------------------------
# notes-history
# ---------------------------------------------------------------------


def test_notes_history_prints_newest_first(backend_db, runner):
    task = backend_db.add_task(text="History", project_id=PROJECT_ID)
    backend_db.update_task(task["id"], notes="v1", notes_source="cli")
    backend_db.update_task(task["id"], notes="v2", notes_source="cli")

    result = runner.invoke(
        tasks_group, ["notes-history", str(backend_db.get_task_display_id(task["id"]))]
    )

    assert result.exit_code == 0, result.output
    assert "new: v2" in result.output
    assert "new: v1" in result.output
    assert result.output.index("new: v2") < result.output.index("new: v1")
    assert "cli" in result.output


def test_notes_history_prints_markup_like_text_verbatim(backend_db, runner):
    task = backend_db.add_task(text="Markup history", project_id=PROJECT_ID)
    notes = "- [x] done\n[red]x[/red]\n[/]"
    backend_db.update_task(task["id"], notes=notes, notes_source="cli")

    result = runner.invoke(
        tasks_group, ["notes-history", str(backend_db.get_task_display_id(task["id"]))]
    )

    assert result.exit_code == 0, result.output
    assert "- [x] done" in result.output
    assert "[red]x[/red]" in result.output
    assert "[/]" in result.output


def test_notes_history_json_shape(backend_db, runner):
    task = backend_db.add_task(text="History json", project_id=PROJECT_ID)
    backend_db.update_task(task["id"], notes="v1", notes_source="cli")

    result = runner.invoke(tasks_group, ["notes-history", str(task["id"]), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = payload[0]
    assert set(row) == {
        "id", "task_id", "project_id", "old_notes", "new_notes", "source", "timestamp",
    }
    assert row["old_notes"] is None
    assert row["new_notes"] == "v1"
    assert row["source"] == "cli"


def test_notes_history_without_rows_prints_message(backend_db, runner):
    task = backend_db.add_task(text="No history", project_id=PROJECT_ID)

    result = runner.invoke(
        tasks_group, ["notes-history", str(backend_db.get_task_display_id(task["id"]))]
    )

    assert result.exit_code == 0, result.output
    assert "no notes history" in result.output.lower()


# ---------------------------------------------------------------------
# delete keeps the evidence
# ---------------------------------------------------------------------


def test_deleting_task_keeps_notes_history_rows(
    backend_db, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SAFE_MODE", "0")
    task = backend_db.add_task(
        text="Delete me", project_id=PROJECT_ID, notes="before delete"
    )
    backend_db.update_task(task["id"], notes="after delete", notes_source="cli")

    backend_db.delete_task(task["id"])

    rows = _history(backend_db, task["id"])
    assert len(rows) == 1
    assert rows[0]["old_notes"] == "before delete"
    assert rows[0]["new_notes"] == "after delete"
