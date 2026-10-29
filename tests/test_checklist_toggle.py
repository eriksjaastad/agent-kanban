"""Atomic checklist toggle (#7821).

A tick must be a server-side compare-and-swap on one line, never a client
built whole-notes PATCH. These tests pin the CAS primitive in
update_task(expected_notes=...), the line toggle helper, and the
POST /api/tasks/{id}/checklist endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app  # noqa: E402
from db.backend_manager import NotesConflictError  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402
from scripts.utils.checklist import (  # noqa: E402
    ChecklistLineError,
    toggle_checklist_line,
)

PROJECT_ID = "project-tracker"
NOTES = "intro\n- [ ] first\n  - [x] second\n- [ ] third\nouttro"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    manager = DatabaseManager()
    manager.add_project(
        project_id=PROJECT_ID, name="PT", path="/tmp/project-tracker", status="active"
    )
    return manager


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(dashboard_app.app)


def _history(db, task_id):
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT old_notes, new_notes, source FROM task_notes_history "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]


_LOAD = object()


def _post(client, task_id, line_index, text, checked, base=_LOAD):
    """POST a toggle. By default ``base_notes`` is what a client would have
    rendered: the notes as GET /api/tasks/{id} returns them right now."""
    if base is _LOAD:
        got = client.get(f"/api/tasks/{task_id}")
        base = got.json().get("notes") if got.status_code == 200 else None
    return client.post(
        f"/api/tasks/{task_id}/checklist",
        json={"line_index": line_index, "expected_text": text, "checked": checked,
              "base_notes": base},
    )


# --- CAS primitive ---------------------------------------------------


def test_cas_success_writes_one_history_row(db):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes="a")
    db.update_task(task["id"], notes_source="api", notes="b", expected_notes="a")
    assert db.get_task(task["id"])["notes"] == "b"
    rows = _history(db, task["id"])
    assert len(rows) == 1
    assert (rows[0]["old_notes"], rows[0]["new_notes"], rows[0]["source"]) == ("a", "b", "api")


def test_cas_mismatch_writes_nothing_and_raises(db):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes="a")
    before = db.get_task(task["id"])
    with pytest.raises(NotesConflictError):
        db.update_task(task["id"], notes_source="api", notes="b", expected_notes="stale")
    after = db.get_task(task["id"])
    assert after["notes"] == "a"
    assert after["updated_at"] == before["updated_at"]
    assert _history(db, task["id"]) == []


def test_cas_none_matches_null_notes(db):
    task = db.add_task(text="t", project_id=PROJECT_ID)
    db.update_task(task["id"], notes="x", expected_notes=None)
    assert db.get_task(task["id"])["notes"] == "x"


def test_update_without_expected_notes_unchanged(db):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes="a")
    db.update_task(task["id"], notes="z")
    assert db.get_task(task["id"])["notes"] == "z"


# --- pure line toggle ------------------------------------------------


def test_toggle_helper_changes_only_marker():
    out = toggle_checklist_line(NOTES, 1, "first", True)
    assert out == NOTES.replace("- [ ] first", "- [x] first")
    out = toggle_checklist_line(NOTES, 2, "second", False)
    assert out == NOTES.replace("  - [x] second", "  - [ ] second")


@pytest.mark.parametrize("marker", ["x", "X"])
def test_toggle_helper_leaves_an_already_checked_marker_alone(marker):
    notes = f"a\n- [{marker}] done"
    assert toggle_checklist_line(notes, 1, "done", True) == notes


def test_endpoint_no_op_on_uppercase_checked_writes_nothing(db, client):
    notes = "- [X] done"
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=notes)
    resp = _post(client, task["id"], 0, "done", True)
    assert resp.status_code == 200, resp.text
    assert db.get_task(task["id"])["notes"] == notes
    assert _history(db, task["id"]) == []


def test_toggle_helper_preserves_crlf():
    notes = "a\r\n- [ ] one\r\n- [ ] two\r\n"
    out = toggle_checklist_line(notes, 1, "one", True)
    assert out == "a\r\n- [x] one\r\n- [ ] two\r\n"


@pytest.mark.parametrize("idx,text", [(0, "intro"), (1, "wrong"), (99, "first"), (-1, "first")])
def test_toggle_helper_rejects_bad_target(idx, text):
    with pytest.raises(ChecklistLineError):
        toggle_checklist_line(NOTES, idx, text, True)


# --- endpoint --------------------------------------------------------


def test_endpoint_toggles_exactly_one_line(db, client):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    resp = _post(client, task["id"], 1, "first", True)
    assert resp.status_code == 200, resp.text
    expected = NOTES.replace("- [ ] first", "- [x] first")
    assert resp.json()["notes"] == expected
    assert db.get_task(task["id"])["notes"] == expected
    rows = _history(db, task["id"])
    assert len(rows) == 1 and rows[0]["source"] == "api"
    assert rows[0]["old_notes"] == NOTES


def test_endpoint_preserves_crlf_bytes(db, client):
    notes = "a\r\n- [ ] one\r\n- [ ] two"
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=notes)
    resp = _post(client, task["id"], 2, "two", True)
    assert resp.status_code == 200, resp.text
    assert db.get_task(task["id"])["notes"] == "a\r\n- [ ] one\r\n- [x] two"


@pytest.mark.parametrize(
    ("idx", "text"),
    [
        (0, "intro"),
        (4, "intro"),
        (50, "intro"),
        (-1, "intro"),
        (1, "something else"),  # a real checklist line, but the text mismatches
    ],
)
def test_endpoint_409_on_non_checklist_or_out_of_range(db, client, idx, text):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    assert _post(client, task["id"], idx, text, True).status_code == 409
    assert db.get_task(task["id"])["notes"] == NOTES
    assert _history(db, task["id"]) == []


def test_endpoint_404_missing_task(client):
    assert _post(client, 123456789, 0, "x", True).status_code == 404


def test_two_sequential_toggles_both_survive(db, client):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    assert _post(client, task["id"], 1, "first", True).status_code == 200
    assert _post(client, task["id"], 3, "third", True).status_code == 200
    notes = db.get_task(task["id"])["notes"]
    assert "- [x] first" in notes and "- [x] third" in notes
    assert len(_history(db, task["id"])) == 2


def test_writer_between_read_and_write_is_never_overwritten(db, client, monkeypatch):
    """A writer landing between the endpoint's read and its write wins: the
    CAS fails, the re-read no longer matches the client's base, and the
    toggle answers 409 without touching the other writer's notes."""
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    backend = DatabaseManager()
    real = type(backend).update_task
    late = NOTES + "\n- [ ] late"

    def racing(self, task_id, *a, **kw):
        real(self, task_id, notes=late, notes_source="cli")
        return real(self, task_id, *a, **kw)

    monkeypatch.setattr(type(backend), "update_task", racing)
    resp = _post(client, task["id"], 1, "first", True)
    assert resp.status_code == 409, resp.text
    assert db.get_task(task["id"])["notes"] == late


def test_stale_base_is_409_and_writes_nothing(db, client):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    assert _post(client, task["id"], 1, "first", True).status_code == 200
    # A second client still holding the original notes ticks "third".
    resp = _post(client, task["id"], 3, "third", True, base=NOTES)
    assert resp.status_code == 409
    notes = db.get_task(task["id"])["notes"]
    assert "- [x] first" in notes and "- [ ] third" in notes
    assert len(_history(db, task["id"])) == 1


def test_identical_items_that_moved_are_409_not_a_wrong_toggle(db, client):
    """Index + text cannot tell duplicates apart; the base notes can."""
    seen = "- [x] A\n- [ ] A"
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=seen)
    # Another writer prepends a third identical item: line 1 is now the
    # already-ticked A, so an index+text match would silently do nothing.
    moved = "- [ ] A\n" + seen
    db.update_task(task["id"], notes=moved)
    resp = _post(client, task["id"], 1, "A", True, base=seen)
    assert resp.status_code == 409
    assert db.get_task(task["id"])["notes"] == moved


def test_null_and_empty_base_match_empty_notes_only(db, client):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    assert _post(client, task["id"], 1, "first", True, base=None).status_code == 409


@pytest.mark.parametrize("when", ["before_update", "after_update", "final_attempt"])
def test_endpoint_404_when_task_deleted_during_request(db, client, monkeypatch, when):
    """A concurrent delete between our read and the write is a 404, not a 500."""
    monkeypatch.setenv("SAFE_MODE", "0")  # let the simulated client delete
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    backend = DatabaseManager()
    real = type(backend).update_task
    calls = {"n": 0}

    def deleting(self, task_id, *a, **kw):
        if when == "final_attempt":
            # every attempt loses the CAS race; the last one also finds the row gone
            calls["n"] += 1
            if calls["n"] == dashboard_app.CHECKLIST_TOGGLE_MAX_ATTEMPTS:
                self.delete_task(task_id)
            raise NotesConflictError("lost the race")
        if when == "before_update":
            self.delete_task(task_id)
            return real(self, task_id, *a, **kw)
        real(self, task_id, *a, **kw)
        self.delete_task(task_id)
        return None  # update_task's own post-commit re-read finds no row

    monkeypatch.setattr(type(backend), "update_task", deleting)
    resp = _post(client, task["id"], 1, "first", True)
    assert resp.status_code == 404, resp.text


def test_endpoint_409_when_cas_keeps_conflicting(db, client, monkeypatch):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    backend = DatabaseManager()

    def always(self, *a, **kw):
        raise NotesConflictError("nope")

    monkeypatch.setattr(type(backend), "update_task", always)
    assert _post(client, task["id"], 1, "first", True).status_code == 409


def test_endpoint_handles_snowflake_ids(db, client, monkeypatch):
    big = 98969975881101312
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    with db._get_conn() as conn:
        conn.execute("UPDATE tasks SET id = ? WHERE id = ?", (big, task["id"]))
        conn.commit()
    resp = _post(client, str(big), 1, "first", True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(big)


# --- edit Save: PATCH with base_notes ------------------------------------


def test_patch_with_current_base_notes_saves(db, client):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    resp = client.patch(
        f"/api/tasks/{task['id']}", json={"notes": "mine", "base_notes": NOTES}
    )
    assert resp.status_code == 200, resp.text
    assert db.get_task(task["id"])["notes"] == "mine"


def test_patch_with_stale_base_notes_is_409_and_writes_nothing(db, client):
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES, priority="Low")
    resp = client.patch(
        f"/api/tasks/{task['id']}",
        json={"notes": "mine", "base_notes": "what the edit started from", "priority": "High"},
    )
    assert resp.status_code == 409
    stored = db.get_task(task["id"])
    assert stored["notes"] == NOTES and stored["priority"] == "Low"
    assert _history(db, task["id"]) == []


@pytest.mark.parametrize("case", ["cancelled", "blocked"])
def test_stale_base_notes_is_409_before_any_status_validation(db, client, case):
    # Another writer changed the notes and either cancelled the task (Done is
    # invalid from Cancelled) or left it blocked (the start gate refuses In
    # Progress). The edit was built on stale notes, so 409 is the answer.
    if case == "cancelled":
        task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
        db.update_task(task["id"], status="Cancelled")
        move = "Done"
    else:
        blocker = db.add_task(text="blocker", project_id=PROJECT_ID)
        task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES, blocked_by=[blocker["id"]])
        move = "In Progress"
    before = db.get_task(task["id"])["status"]
    resp = client.patch(
        f"/api/tasks/{task['id']}",
        json={"notes": "mine", "base_notes": "what the edit started from", "status": move},
    )
    assert resp.status_code == 409, resp.text
    assert db.get_task(task["id"])["status"] == before


def test_patch_writer_between_precondition_and_write_wins(db, client, monkeypatch):
    # The handler's early check passed; another writer lands before the
    # UPDATE. The SQL CAS must still refuse the edit.
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    real = DatabaseManager.update_task
    late = NOTES + "\n- [ ] late"

    def racing(self, task_id, *a, **kw):
        real(self, task_id, notes=late, notes_source="cli")
        return real(self, task_id, *a, **kw)

    monkeypatch.setattr(DatabaseManager, "update_task", racing)
    resp = client.patch(f"/api/tasks/{task['id']}", json={"notes": "mine", "base_notes": NOTES})
    assert resp.status_code == 409, resp.text
    assert db.get_task(task["id"])["notes"] == late


def test_patch_with_null_base_notes_conflicts_with_existing_notes(db, client):
    # The edit started from a task with no notes; an agent has since added some.
    task = db.add_task(text="t", project_id=PROJECT_ID, notes=NOTES)
    resp = client.patch(
        f"/api/tasks/{task['id']}", json={"notes": "mine", "base_notes": None}
    )
    assert resp.status_code == 409
    assert db.get_task(task["id"])["notes"] == NOTES
