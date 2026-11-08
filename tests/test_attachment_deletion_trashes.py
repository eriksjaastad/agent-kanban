"""Attachment files must be trashed, never unlinked (#6905).

Both delete paths destroyed user-uploaded content outright, so a misclick in
the dashboard removed a file with no recovery. The owner's standing rule: send2trash
for anything that is not a temp file the same process just created.

These tests exist because the fix shipped with no coverage at all — nothing
stopped a later edit quietly restoring `.unlink()`. They assert the mechanism,
not just the outcome, since "the file is gone" is true either way.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dashboard.app import app  # noqa: E402
from db.attachment_paths import delete_attachment_files  # noqa: E402
from db.manager import DatabaseManager  # noqa: E402

client = TestClient(app)


def test_bulk_cleanup_trashes_files_rather_than_unlinking(tmp_path, monkeypatch):
    """The cascade path (`_delete_attachment_files`) must use send2trash."""
    attach_dir = tmp_path / "7"
    attach_dir.mkdir(parents=True)
    stored = attach_dir / "invoice.pdf"
    stored.write_bytes(b"user content")

    # `attachments_dir` moved out of DatabaseManager into
    # db.attachment_paths when the manager became an RPC proxy. Both the
    # backend and the dashboard import it by name, so both bindings are
    # patched — patching only the module would leave dashboard.app holding
    # the original and the test would silently stop testing anything.
    monkeypatch.setattr(
        "db.attachment_paths.attachments_dir",
        lambda task_id, create=True: attach_dir,
    )
    monkeypatch.setattr(
        "dashboard.routes.tasks.attachments_dir",
        lambda task_id, create=True: attach_dir,
        raising=False,
    )

    with patch("send2trash.send2trash") as trash:
        with patch.object(Path, "unlink", side_effect=AssertionError(
            "attachment was unlinked instead of trashed — user data destroyed"
        )):
            delete_attachment_files(
                [{"task_id": 7, "stored_name": "invoice.pdf"}]
            )

    trash.assert_called_once_with(str(stored))


def test_bulk_cleanup_survives_a_trash_failure(tmp_path, monkeypatch):
    """One unreachable file must not abort cleanup of the rest."""
    attach_dir = tmp_path / "7"
    attach_dir.mkdir(parents=True)
    (attach_dir / "a.pdf").write_bytes(b"a")
    (attach_dir / "b.pdf").write_bytes(b"b")

    # `attachments_dir` moved out of DatabaseManager into
    # db.attachment_paths when the manager became an RPC proxy. Both the
    # backend and the dashboard import it by name, so both bindings are
    # patched — patching only the module would leave dashboard.app holding
    # the original and the test would silently stop testing anything.
    monkeypatch.setattr(
        "db.attachment_paths.attachments_dir",
        lambda task_id, create=True: attach_dir,
    )
    monkeypatch.setattr(
        "dashboard.routes.tasks.attachments_dir",
        lambda task_id, create=True: attach_dir,
        raising=False,
    )

    calls = []

    def flaky(target):
        calls.append(target)
        if target.endswith("a.pdf"):
            raise OSError("trash unavailable")

    with patch("send2trash.send2trash", side_effect=flaky):
        delete_attachment_files([
            {"task_id": 7, "stored_name": "a.pdf"},
            {"task_id": 7, "stored_name": "b.pdf"},
        ])

    assert len(calls) == 2, "a failure on the first file stopped the second"


def test_delete_endpoint_trashes_rather_than_unlinking(tmp_path, monkeypatch):
    """The DELETE endpoint must use send2trash too — it was the path a user
    actually clicks."""
    attach_dir = tmp_path / "42"
    attach_dir.mkdir(parents=True)
    stored = attach_dir / "receipt.png"
    stored.write_bytes(b"user content")

    # `attachments_dir` moved out of DatabaseManager into
    # db.attachment_paths when the manager became an RPC proxy. Both the
    # backend and the dashboard import it by name, so both bindings are
    # patched — patching only the module would leave dashboard.app holding
    # the original and the test would silently stop testing anything.
    monkeypatch.setattr(
        "db.attachment_paths.attachments_dir",
        lambda task_id, create=True: attach_dir,
    )
    monkeypatch.setattr(
        "dashboard.routes.tasks.attachments_dir",
        lambda task_id, create=True: attach_dir,
        raising=False,
    )
    monkeypatch.setattr(
        DatabaseManager, "delete_attachment", raising=False, value=
        lambda self, attachment_id, task_id: {"stored_name": "receipt.png"},
    )

    with patch("send2trash.send2trash") as trash:
        with patch.object(Path, "unlink", side_effect=AssertionError(
            "attachment was unlinked instead of trashed — user data destroyed"
        )):
            resp = client.delete("/api/tasks/42/attachments/1")

    assert resp.status_code == 200
    trash.assert_called_once_with(str(stored))


def test_delete_endpoint_reports_an_orphan_instead_of_500ing(tmp_path, monkeypatch):
    """If trashing fails the DB row is already committed, so raising would
    500 while leaving the file orphaned and invisible. Report it instead."""
    attach_dir = tmp_path / "42"
    attach_dir.mkdir(parents=True)
    (attach_dir / "receipt.png").write_bytes(b"user content")

    # `attachments_dir` moved out of DatabaseManager into
    # db.attachment_paths when the manager became an RPC proxy. Both the
    # backend and the dashboard import it by name, so both bindings are
    # patched — patching only the module would leave dashboard.app holding
    # the original and the test would silently stop testing anything.
    monkeypatch.setattr(
        "db.attachment_paths.attachments_dir",
        lambda task_id, create=True: attach_dir,
    )
    monkeypatch.setattr(
        "dashboard.routes.tasks.attachments_dir",
        lambda task_id, create=True: attach_dir,
        raising=False,
    )
    monkeypatch.setattr(
        DatabaseManager, "delete_attachment", raising=False, value=
        lambda self, attachment_id, task_id: {"stored_name": "receipt.png"},
    )

    with patch("send2trash.send2trash", side_effect=OSError("trash unavailable")):
        resp = client.delete("/api/tasks/42/attachments/1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert "file_orphaned" in body, "a silently orphaned file is how files get lost"


def test_missing_attachment_still_404s(monkeypatch):
    monkeypatch.setattr(
        DatabaseManager, "delete_attachment", raising=False, value=
        lambda self, attachment_id, task_id: None,
    )
    assert client.delete("/api/tasks/42/attachments/999").status_code == 404
