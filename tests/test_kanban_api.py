"""Property-based tests for the Kanban board API, run against dashboard.app.

Feature: kanban-board. Properties tested:
- Property 8: Task Edit Persistence
- Property 18: Transaction Rollback on API Error
- Property 19: Database Lock Retry

Until #8735 these ran against a copy of the routes defined in this file, so a
change to the real routes could not fail them. They now use the app itself,
on conftest's per-test database, and inject failures into the class the app
actually imports (db.backend_manager, not scripts.db.backend_manager).
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import dashboard.app as dashboard_app
from db.backend_manager import DatabaseManager as AppBackend
from db.manager import DatabaseManager
from scripts.utils.validation import (
    contains_secret,
    get_blocked_card_project_ids,
    is_card_creation_allowed,
    validate_task_input,
)

PROJECT_IDS = ["project-tracker", "image-workflow", "trading-desk", "test-project-1", "test-project-2"]
# Checked by the eligibility test; not a generator project.
EXTRA_PROJECT = "smart-invoice-workflow"
NOTES = "- [ ] the edit is visible after a reload"

# Every example does real SQLite I/O, so Hypothesis's 200ms deadline measured
# disk and machine load, not this code (#7896). The examples share the test's
# database, which is fine: each one creates its own task.
DB_PROPERTY_SETTINGS = {"deadline": None, "suppress_health_check": [HealthCheck.function_scoped_fixture]}
texts = st.text(min_size=1, max_size=100).map(str.strip).filter(bool)
statuses = st.sampled_from(["Backlog", "To Do", "In Progress", "Done"])
priorities = st.one_of(st.none(), st.sampled_from(["Critical", "High", "Medium", "Low"]))


@pytest.fixture
def client(isolated_database):
    db = DatabaseManager()
    for project_id in [*PROJECT_IDS, EXTRA_PROJECT]:
        db.add_project(project_id=project_id, name=project_id, path=f"/tmp/{project_id}", status="active")
    return TestClient(dashboard_app.app)


def create(client, text, project_id, status, priority):
    response = client.post("/api/tasks", json={
        "text": text, "project_id": project_id, "status": status, "priority": priority, "notes": NOTES,
    })
    assert response.status_code == 201, response.text
    return response.json()


def get(client, path):
    """GET path from the app, asserting it answered 200."""
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def task_count(client):
    return len(get(client, "/api/tasks")["tasks"])


@pytest.mark.parametrize("project_id", ["project-tracker", EXTRA_PROJECT])
def test_no_project_is_blocked_on_the_macbook(client, project_id):
    """With the Mac Mini disconnected, every project accepts cards, through the helpers and the API."""
    assert get_blocked_card_project_ids() == []
    assert is_card_creation_allowed(project_id) is True
    is_valid, error = validate_task_input(
        text="Cards should work everywhere", project_id=project_id,
        status="Backlog", priority=None, db_manager=DatabaseManager(),
    )
    assert is_valid and error is None
    created = create(client, "Cards should work everywhere", project_id, "Backlog", None)
    assert get(client, f"/api/tasks/{created['id']}")["project_id"] == project_id


# Property 8: Task Edit Persistence. Editing a task's text through the API
# updates it and its updated_at, leaves the other fields alone, and survives
# a reload.
@given(initial_text=texts, new_text=texts, project_id=st.sampled_from(PROJECT_IDS),
       status=statuses, priority=priorities)
@settings(max_examples=100, **DB_PROPERTY_SETTINGS)
def test_property_8_task_edit_persistence(client, initial_text, new_text, project_id, status, priority):
    assume(not contains_secret(initial_text)[0] and not contains_secret(new_text)[0])
    assume(initial_text != new_text)
    created = create(client, initial_text, project_id, status, priority)
    time.sleep(0.01)  # so updated_at can differ

    response = client.patch(f"/api/tasks/{created['id']}", json={"text": new_text})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["text"] == new_text
    assert updated["updated_at"] != created["updated_at"]
    datetime.fromisoformat(updated["updated_at"])
    assert (updated["project_id"], updated["status"], updated["priority"], updated["created_at"]) == (
        project_id, status, priority, created["created_at"])

    reloaded = get(client, f"/api/tasks/{created['id']}")
    assert reloaded["text"] == new_text
    assert reloaded["updated_at"] == updated["updated_at"]


# Property 18: Transaction Rollback on API Error. A write that fails at commit
# leaves the database as it was before the request.
@given(text=texts, project_id=st.sampled_from(PROJECT_IDS), status=statuses, priority=priorities)
@settings(max_examples=50, **DB_PROPERTY_SETTINGS)
def test_property_18_transaction_rollback_on_api_error(client, text, project_id, status, priority):
    assume(not contains_secret(text)[0])
    initial = task_count(client)
    created = create(client, text, project_id, status, priority)
    assert task_count(client) == initial + 1
    seen = {"update": False, "commit_failed": False}

    class Cursor:
        """A real cursor that notes UPDATEs; sqlite3.Cursor.execute can't be reassigned."""

        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, sql, *args):
            seen["update"] |= sql.strip().upper().startswith("UPDATE")
            return self._cursor.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class Conn:
        """A real connection whose commit fails once an UPDATE has run."""

        def __init__(self, conn):
            self._conn = conn

        def cursor(self):
            return Cursor(self._conn.cursor())

        def commit(self):
            if seen["update"]:
                seen["commit_failed"] = True
                raise sqlite3.OperationalError("Simulated database error during commit")
            self._conn.commit()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def commit_fails_after_update(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield Conn(conn)
        finally:
            conn.close()

    with patch.object(AppBackend, "_get_conn", commit_fails_after_update):
        response = client.patch(f"/api/tasks/{created['id']}", json={"text": "This should not persist"})
    assert seen == {"update": True, "commit_failed": True}, "the injected failure never reached the commit"
    assert response.status_code in (400, 500), response.text
    assert task_count(client) == initial + 1
    assert get(client, f"/api/tasks/{created['id']}")["text"] == text


# Property 19: Database Lock Retry. An edit that meets a locked database
# backs off and retries until the lock is gone.
@given(text=texts, project_id=st.sampled_from(PROJECT_IDS), status=statuses, priority=priorities)
@settings(max_examples=1, **DB_PROPERTY_SETTINGS)
def test_property_19_database_lock_retry(client, text, project_id, status, priority):
    assume(not contains_secret(text)[0])
    created = create(client, text, project_id, status, priority)
    db_path = DatabaseManager().db_path
    lock = sqlite3.connect(db_path, check_same_thread=False)
    lock.execute("BEGIN EXCLUSIVE TRANSACTION")
    first_backoff = threading.Event()
    sleeps = []
    real_sleep = time.sleep

    def recording_sleep(seconds):
        sleeps.append(seconds)
        first_backoff.set()
        real_sleep(seconds)

    def release_after_first_backoff():
        first_backoff.wait(timeout=0.2)
        lock.rollback()
        lock.close()

    @contextmanager
    def no_busy_wait(self):
        conn = sqlite3.connect(self.db_path, timeout=0.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    releaser = threading.Thread(target=release_after_first_backoff)
    releaser.start()
    try:
        with patch.object(AppBackend, "_get_conn", no_busy_wait), patch("time.sleep", side_effect=recording_sleep):
            response = client.patch(f"/api/tasks/{created['id']}", json={"text": "Updated with retry"})
    finally:
        releaser.join(timeout=1)
    assert response.status_code == 200, response.text
    assert sleeps, "expected a retry backoff while the database was locked"
    assert get(client, f"/api/tasks/{created['id']}")["text"] == "Updated with retry"
