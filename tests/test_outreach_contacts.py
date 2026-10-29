"""Outreach contacts: local-only migration, manager mixin, and dashboard API (#7641)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.migration_runner import discover_migrations  # noqa: E402
from db.outreach import ContactStateConflictError  # noqa: E402
from db.schema import schema_statements  # noqa: E402
from dashboard.app import app  # noqa: E402


MIGRATIONS_DIR = Path(__file__).parent.parent / "scripts" / "db" / "migrations"


def _migration_up(conn: sqlite3.Connection) -> None:
    migration = next(
        m for m in discover_migrations(MIGRATIONS_DIR) if m.version == 15
    )
    migration.up(conn)


def _client() -> TestClient:
    return TestClient(app)


def _add(client: TestClient, name: str) -> dict:
    response = client.post("/api/outreach/contacts", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["contact"]


def _contacted(client: TestClient, contact_id: int) -> dict:
    response = client.post(f"/api/outreach/contacts/{contact_id}/contacted")
    assert response.status_code == 200, response.text
    return response.json()["contact"]


# ---------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------


def test_outreach_migration_is_additive_idempotent_and_preserves_data(
    tmp_path: Path,
) -> None:
    original = sqlite3.connect(tmp_path / "original.db")
    original.execute("CREATE TABLE existing_record (value TEXT NOT NULL)")
    original.execute("INSERT INTO existing_record VALUES ('retained')")
    original.commit()
    copy = sqlite3.connect(tmp_path / "copy.db")
    original.backup(copy)
    original.close()

    _migration_up(copy)
    _migration_up(copy)
    assert copy.execute("SELECT value FROM existing_record").fetchall() == [
        ("retained",)
    ]
    assert {
        row[0]
        for row in copy.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    } >= {"outreach_contacts", "existing_record"}
    copy.close()


def test_outreach_columns_and_index(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "outreach.db")
    _migration_up(conn)

    names = [row[1] for row in conn.execute("PRAGMA table_info(outreach_contacts)")]
    assert names == [
        "id", "name", "created_at", "contacted_at", "replied_at",
        "deleted_at", "updated_at",
    ]
    index_names = {
        row[1] for row in conn.execute("PRAGMA index_list(outreach_contacts)")
    }
    assert "idx_outreach_contacts_deleted_replied" in index_names
    index_columns = [
        row[2]
        for row in conn.execute(
            "PRAGMA index_info(idx_outreach_contacts_deleted_replied)"
        )
    ]
    assert index_columns == ["deleted_at", "replied_at"]

    conn.execute(
        "INSERT INTO outreach_contacts (name, created_at, updated_at) "
        "VALUES ('Ada', '2026-09-26T00:00:00+00:00', '2026-09-26T00:00:00+00:00')"
    )
    assert conn.execute(
        "SELECT contacted_at FROM outreach_contacts"
    ).fetchone()[0] is None
    conn.close()


def test_fresh_schema_matches_migration() -> None:
    conn = sqlite3.connect(":memory:")
    for statement in schema_statements():
        conn.execute(statement)
    _migration_up(conn)

    migrated = sqlite3.connect(":memory:")
    _migration_up(migrated)
    for table in ("outreach_contacts",):
        assert conn.execute(f"PRAGMA table_info({table})").fetchall() == (
            migrated.execute(f"PRAGMA table_info({table})").fetchall()
        )
        # index_list follows creation order, which differs between the paths.
        assert sorted(r[1:] for r in conn.execute(f"PRAGMA index_list({table})")) == (
            sorted(r[1:] for r in migrated.execute(f"PRAGMA index_list({table})"))
        )
    conn.close()
    migrated.close()


# ---------------------------------------------------------------------
# Mixin + API
# ---------------------------------------------------------------------


def test_add_strips_names_and_rejects_empty(db) -> None:
    client = _client()
    response = client.post("/api/outreach/contacts", json={"name": "  Alice  "})
    assert response.status_code == 201, response.text
    contact = response.json()["contact"]
    assert contact["name"] == "Alice"
    assert contact["contacted_at"] is None
    assert contact["created_at"]
    assert contact["updated_at"]

    assert client.post(
        "/api/outreach/contacts", json={"name": "   "}
    ).status_code == 400

    assert [row["id"] for row in client.get("/api/outreach/contacts").json()["contacts"]] == [
        contact["id"]
    ]

    with pytest.raises(ValueError):
        db.add_contact("   ")
    assert db.add_contact("  Bob  ")["name"] == "Bob"


def test_list_active_contacts_orders_uncontacted_then_contacted(db) -> None:
    client = _client()
    first = _add(client, "First")
    second = _add(client, "Second")
    third = _add(client, "Third")

    _contacted(client, second["id"])

    ids = [row["id"] for row in client.get("/api/outreach/contacts").json()["contacts"]]
    assert ids == [first["id"], third["id"], second["id"]]


def test_rename_strips_updates_and_handles_unknown_or_empty(db) -> None:
    client = _client()
    contact = _add(client, "Original")

    response = client.patch(
        f"/api/outreach/contacts/{contact['id']}", json={"name": "  Renamed  "}
    )
    assert response.status_code == 200, response.text
    assert response.json()["contact"]["name"] == "Renamed"

    assert client.patch(
        f"/api/outreach/contacts/{contact['id']}", json={"name": "   "}
    ).status_code == 400
    assert client.patch(
        "/api/outreach/contacts/999999", json={"name": "Ghost"}
    ).status_code == 404

    with pytest.raises(ValueError):
        db.rename_contact(contact["id"], "   ")
    assert db.rename_contact(contact["id"], "  Again  ")["name"] == "Again"
    assert db.rename_contact(999999, "Ghost") is None


def test_rename_allowed_after_contacted(db) -> None:
    client = _client()
    contact = _add(client, "Reach out")
    contacted_at = _contacted(client, contact["id"])["contacted_at"]

    response = client.patch(
        f"/api/outreach/contacts/{contact['id']}", json={"name": "  Reached out  "}
    )
    assert response.status_code == 200, response.text
    renamed = response.json()["contact"]
    assert renamed["name"] == "Reached out"
    assert renamed["contacted_at"] == contacted_at
    assert renamed["replied_at"] is None

    listed = client.get("/api/outreach/contacts").json()["contacts"]
    assert [(row["name"], row["contacted_at"]) for row in listed] == [
        ("Reached out", contacted_at)
    ]


def test_rename_rejected_after_replied(db) -> None:
    client = _client()
    contact = _add(client, "Done deal")
    _contacted(client, contact["id"])
    replied = client.post(f"/api/outreach/contacts/{contact['id']}/replied")
    assert replied.status_code == 200, replied.text

    response = client.patch(
        f"/api/outreach/contacts/{contact['id']}", json={"name": "Nope"}
    )
    assert response.status_code == 409
    with pytest.raises(ContactStateConflictError):
        db.rename_contact(contact["id"], "Nope")


def test_contacted_twice_conflicts(db) -> None:
    client = _client()
    contact = _add(client, "Once")
    _contacted(client, contact["id"])

    assert client.post(
        f"/api/outreach/contacts/{contact['id']}/contacted"
    ).status_code == 409
    with pytest.raises(ValueError):
        db.mark_contacted(contact["id"])


def test_replied_removes_from_list_but_keeps_row(db) -> None:
    client = _client()
    kept = _add(client, "Kept")
    finished = _add(client, "Finished")
    _contacted(client, finished["id"])

    response = client.post(f"/api/outreach/contacts/{finished['id']}/replied")
    assert response.status_code == 200, response.text
    assert response.json()["contact"]["replied_at"]

    ids = [row["id"] for row in client.get("/api/outreach/contacts").json()["contacts"]]
    assert ids == [kept["id"]]

    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT replied_at FROM outreach_contacts WHERE id = ?",
            (finished["id"],),
        ).fetchone()
    assert row is not None and row["replied_at"] is not None


def test_replied_before_contacted_conflicts(db) -> None:
    client = _client()
    contact = _add(client, "Too soon")

    assert client.post(
        f"/api/outreach/contacts/{contact['id']}/replied"
    ).status_code == 409
    with pytest.raises(ValueError):
        db.mark_replied(contact["id"])


def test_delete_soft_deletes_and_unknown_ids_return_404(db) -> None:
    client = _client()
    contact = _add(client, "Delete me")

    response = client.delete(f"/api/outreach/contacts/{contact['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["contact"]["deleted_at"]

    assert client.get("/api/outreach/contacts").json()["contacts"] == []
    assert client.delete(f"/api/outreach/contacts/{contact['id']}").status_code == 404

    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM outreach_contacts WHERE id = ?",
            (contact["id"],),
        ).fetchone()
    assert row is not None and row["deleted_at"] is not None

    assert client.delete("/api/outreach/contacts/999999").status_code == 404
    assert client.post("/api/outreach/contacts/999999/contacted").status_code == 404
    assert client.post("/api/outreach/contacts/999999/replied").status_code == 404
    assert db.soft_delete_contact(999999) is None
