"""Snowflake-scale task ids must not lose precision on the wire (#7824).

pt_next_id (#6044) generates 63-bit ids that routinely exceed
Number.MAX_SAFE_INTEGER (2^53-1 == 9007199254740991). Python's own json
encoder preserves full precision for an int, but every browser's
`JSON.parse` silently rounds an unquoted integer literal above that
threshold -- 98969975881101312 becomes 98969975881101310. Every dashboard
PATCH built from a parsed id then hits the wrong row (or 404s outright).

The fix is a response-contract change: the API must emit every
Snowflake-scale id as a decimal *string*, never a bare JSON number.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402
from db.schema import create_database  # noqa: E402

# Real example from the bug report: exceeds Number.MAX_SAFE_INTEGER
# (9007199254740991) and rounds to 98969975881101310 under JSON.parse.
BIG_ID = 98969975881101312


def _setup_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = DatabaseManager(db_path)
    db.add_project(
        project_id="project-tracker",
        name="Project Tracker",
        path=str(tmp_path / "project-tracker"),
        status="active",
    )
    monkeypatch.setenv("PT_DB_PATH", str(db_path))
    # add_task() is the SQL core's method (backend_manager.DatabaseManager),
    # which calls pt_next_id from db.backend_manager --
    # patch it there, not db.pt_id, or the real generator still runs.
    #
    # A constant return value collides: creating one task also inserts a
    # task_history row via a second pt_next_id() call in the same request,
    # and a repeated id trips that table's UNIQUE constraint. Emit
    # consecutive Snowflake-scale ids instead so every row still gets an
    # id above Number.MAX_SAFE_INTEGER, just not the same one twice.
    counter = itertools.count(BIG_ID)
    monkeypatch.setattr("db.backend_manager.pt_next_id", lambda _db_path: next(counter))
    return db


def test_oversized_task_id_is_a_quoted_string_on_the_wire(tmp_path, monkeypatch):
    db = _setup_db(tmp_path, monkeypatch)
    task = db.add_task("Oversized id task", "project-tracker")
    assert task["id"] == BIG_ID, "fixture precondition: pt_next_id was not patched"

    response = TestClient(dashboard_app.app).get(f"/api/tasks/{BIG_ID}")
    assert response.status_code == 200, response.text

    # Check the raw response TEXT, not response.json(). Python's own
    # json.loads would silently "fix" an unquoted-number regression the
    # same way a browser silently breaks on one -- the contract lives in
    # the bytes the server sent, not in how a second Python parser re-reads
    # them.
    compact = response.text.replace(" ", "").replace("\n", "")
    assert f'"id":"{BIG_ID}"' in compact, (
        "task id must be a quoted JSON string on the wire -- a bare number "
        "above 2^53-1 rounds in every browser's JSON.parse (#7824)"
    )
    assert response.json()["id"] == str(BIG_ID)
    assert isinstance(response.json()["id"], str)


def test_oversized_task_id_patches_the_exact_row(tmp_path, monkeypatch):
    db = _setup_db(tmp_path, monkeypatch)
    task = db.add_task("Oversized id task", "project-tracker")
    assert task["id"] == BIG_ID

    client = TestClient(dashboard_app.app)
    # Exactly what the browser sends: the full-precision decimal string a
    # fixed frontend would read back from the GET response above.
    response = client.patch(f"/api/tasks/{BIG_ID}", json={"status": "To Do"})

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(BIG_ID)
    assert db.get_task(BIG_ID)["status"] == "To Do", (
        "PATCH against the exact-digit id must land on the row pt_next_id created"
    )


def test_oversized_parent_and_blocked_by_ids_are_strings(tmp_path, monkeypatch):
    """parent_id, blocked_by_ids and incomplete_blocking_ids all carry the
    same Snowflake-scale ids and must get the same string treatment."""
    db = _setup_db(tmp_path, monkeypatch)
    parent = db.add_task("Parent", "project-tracker")
    assert parent["id"] == BIG_ID
    assert parent["id"] > 2**53 - 1  # fixture precondition: genuinely oversized

    child = db.add_task(
        "Child", "project-tracker", parent_id=parent["id"], blocked_by=[parent["id"]],
    )
    assert child["id"] > 2**53 - 1  # fixture precondition

    response = TestClient(dashboard_app.app).get(f"/api/tasks/{child['id']}")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["id"] == str(child["id"])
    assert payload["parent_id"] == str(parent["id"])
    assert payload["blocked_by_ids"] == [str(parent["id"])]
    assert all(isinstance(v, str) for v in payload["blocked_by_ids"])
