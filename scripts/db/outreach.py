"""Private outreach contacts for the Morning page (#7641).

Names in this module are personal data. Do not log them. The dashboard API is
the only consumer that returns them; the CLI never prints them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContactStateConflictError(ValueError):
    """The contact's current state blocks the requested operation.

    Subclasses ValueError so existing callers that treat any bad request as a
    ValueError keep working, while the dashboard API can map this to 409.
    """


class OutreachMixin:
    """Outreach contact operations exposed through the shared DatabaseManager."""

    def list_active_contacts(self) -> list[dict[str, Any]]:
        """Contacts neither deleted nor replied.

        Uncontacted rows come first ordered by created_at/id, then contacted
        rows ordered by contacted_at/id.
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outreach_contacts
                WHERE deleted_at IS NULL AND replied_at IS NULL
                ORDER BY (contacted_at IS NULL) DESC,
                         CASE WHEN contacted_at IS NULL
                              THEN created_at ELSE contacted_at END ASC,
                         id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def add_contact(self, name: str) -> dict[str, Any]:
        """Insert one contact; the name is stripped and must not be empty."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("name is required")
        now = _utc_now()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO outreach_contacts
                    (name, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (cleaned, now, now),
            )
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def rename_contact(self, contact_id: int, name: str) -> dict[str, Any] | None:
        """Rename an active contact, contacted or not; replied rows are immutable."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("name is required")
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                return None
            if row["replied_at"] is not None:
                raise ContactStateConflictError("replied contacts cannot be renamed")
            conn.execute(
                "UPDATE outreach_contacts SET name = ?, updated_at = ? WHERE id = ?",
                (cleaned, _utc_now(), contact_id),
            )
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def mark_contacted(self, contact_id: int) -> dict[str, Any] | None:
        """Move an uncontacted contact into the contacted state."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                return None
            if row["contacted_at"] is not None or row["replied_at"] is not None:
                raise ValueError("contact is already contacted or replied")
            now = _utc_now()
            conn.execute(
                "UPDATE outreach_contacts SET contacted_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, contact_id),
            )
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def mark_replied(self, contact_id: int) -> dict[str, Any] | None:
        """Finish a contacted contact's life cycle; the row is kept."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                return None
            if row["contacted_at"] is None:
                raise ValueError("contact has not been contacted yet")
            if row["replied_at"] is not None:
                raise ValueError("contact already replied")
            now = _utc_now()
            conn.execute(
                "UPDATE outreach_contacts SET replied_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, contact_id),
            )
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def soft_delete_contact(self, contact_id: int) -> dict[str, Any] | None:
        """Soft-delete a contact; the row is never hard-deleted."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                return None
            now = _utc_now()
            conn.execute(
                "UPDATE outreach_contacts SET deleted_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, contact_id),
            )
            row = conn.execute(
                "SELECT * FROM outreach_contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            conn.commit()
            return dict(row)
