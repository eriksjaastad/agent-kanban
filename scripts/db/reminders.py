"""Reminders: things the user wants to remember they have (#8282).

The dashboard's /reminders page and `pt reminders` share these methods. A
reminder is active until it is built into a process, skill or tool
(built_in_at) or removed (deleted_at, a soft delete).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional(text: str | None) -> str | None:
    cleaned = (text or "").strip()
    return cleaned or None


class RemindersMixin:
    """Reminder operations exposed through the shared DatabaseManager."""

    def list_reminders(self) -> dict[str, list[dict[str, Any]]]:
        """Undeleted reminders: active in the order added, built-in newest first."""
        with self._get_conn() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM reminders WHERE deleted_at IS NULL "
                    "ORDER BY built_in_at DESC, id ASC"
                )
            ]
        return {
            "active": [row for row in rows if row["built_in_at"] is None],
            "built_in": [row for row in rows if row["built_in_at"] is not None],
        }

    def add_reminder(
        self, name: str, purpose: str | None = None, location: str | None = None
    ) -> dict[str, Any]:
        """Insert one reminder; the name is stripped and must not be empty."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("name is required")
        now = _utc_now()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO reminders (name, purpose, location, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cleaned, _optional(purpose), _optional(location), now, now),
            )
            row = conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def _retire_reminder(self, reminder_id: int, column: str) -> dict[str, Any] | None:
        """Stamp built_in_at or deleted_at once; None when the reminder is gone.

        The guard is in the UPDATE itself, so a built-in mark racing a
        removal cannot report success on a row that removal already deleted.
        """
        now = _utc_now()
        with self._get_conn() as conn:
            cursor = conn.execute(
                f"UPDATE reminders SET {column} = ?, updated_at = ? "  # noqa: S608 - column is one of two literals
                f"WHERE id = ? AND deleted_at IS NULL AND {column} IS NULL",
                (now, now, reminder_id),
            )
            row = conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            conn.commit()
        # No update: missing, already removed, or (built-in only) already built in.
        if cursor.rowcount == 0 and (row is None or row["deleted_at"] is not None):
            return None
        return dict(row)

    def mark_reminder_built_in(self, reminder_id: int) -> dict[str, Any] | None:
        """Retire a reminder whose content now lives in a process, skill or tool."""
        return self._retire_reminder(reminder_id, "built_in_at")

    def remove_reminder(self, reminder_id: int) -> dict[str, Any] | None:
        """Soft-delete an active or built-in reminder; never hard-deleted."""
        return self._retire_reminder(reminder_id, "deleted_at")
