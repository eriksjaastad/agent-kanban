"""Regression tests for task and admin API behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app
import dashboard.common as dashboard_common
from db.attachment_paths import attachments_dir
from db.manager import DatabaseManager
from db.schema import create_database


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
    return db_path, db


def _request_for_host(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/refresh",
            "headers": [],
            "client": (host, 5000),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_list_tasks_honors_status_query_param(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    done_task = db.add_task("Done task", "project-tracker", status="Done")
    db.add_task("Open task", "project-tracker", status="Backlog")

    response = TestClient(dashboard_app.app).get("/api/tasks?status=Done")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    # str(): the API stringifies Snowflake-scale ids on the wire (#7824);
    # done_task["id"] here is the raw int from the DatabaseManager layer.
    assert [task["id"] for task in payload["tasks"]] == [str(done_task["id"])]


def test_update_task_can_clear_nullable_fields_via_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path, db = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    task = db.add_task("Needs cleanup", "project-tracker", status="Backlog", priority="High")
    db.update_task(task["id"], title="Title", notes="Notes", priority="High")

    response = TestClient(dashboard_app.app).patch(
        f"/api/tasks/{task['id']}",
        json={"title": None, "notes": None, "priority": None},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["title"] is None
    assert payload["notes"] is None
    assert payload["priority"] is None


def test_attachment_download_requires_live_db_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path, db = _setup_db(tmp_path)
    monkeypatch.setenv("PT_DB_PATH", str(db_path))

    task = db.add_task("Task with orphan", "project-tracker")
    attachment_dir = attachments_dir(task["id"])
    (attachment_dir / "orphan.bin").write_text("secret-ish")

    response = TestClient(dashboard_app.app).get(f"/api/attachments/{task['id']}/orphan.bin")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Attachment not found"


def test_require_local_admin_request_blocks_remote_host():
    with pytest.raises(HTTPException, match="restricted to local requests"):
        dashboard_common._require_local_admin_request(_request_for_host("remote.example"))
