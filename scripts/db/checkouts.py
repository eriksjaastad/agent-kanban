"""Project checkouts: which machine a project is being developed on (#8339).

A project checked out to one machine is not developed on the other. Cards
can still be added to it, and the board stays the way to reach the floor
manager working it. `pt project checkout|checkin|checkouts` and the
dashboard's project list share these methods.

Two kinds (#8626): an owned checkout (a project that lives on one machine on purpose)
has no task_id and never gets a reminder; a checkout taken for a card records
that card's task PK, and is due for check-in once the card is Done, Cancelled
or gone, or once both the checkout and the card's last update are older than
CHECKIN_REMINDER_DAYS.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

# Keys are the machine qualifiers agent-chat/identity.py uses.
MACHINE_LABELS = {"laptop": "MacBook", "mini": "Mac Mini"}

# A project once sat checked out to the other machine for a week for one card (#8626).
CHECKIN_REMINDER_DAYS = 3

# What a checkout read returns everywhere but checkin_reminders().
CHECKOUT_COLUMNS = "id, project_id, machine, holder, note, checked_out_at, checked_in_at, checked_in_by"


class ProjectCheckedOut(Exception):
    """A checkout was refused because the project is already out."""

    def __init__(self, checkout: dict[str, Any]):
        self.checkout = checkout
        super().__init__(
            f"{checkout['project_id']} is already checked out to "
            f"{MACHINE_LABELS[checkout['machine']]} by {checkout['holder']} "
            f"since {checkout['checked_out_at'][:10]}"
        )


class CheckoutsMigrationPendingError(ValueError):
    """A checkout or check-in on a database still waiting for migration 023 (or 024)."""

    def __init__(self, migration: str = "023") -> None:
        super().__init__(f"project checkouts need migration {migration}; run `pt db migrate`")


def _has_checkouts_table(conn: Any) -> bool:
    """True once migration 023 has created project_checkouts.

    Numbered migrations reach an existing database only through
    `pt db migrate`; neither the dashboard nor `pt` applies them at startup.
    Until then no project can be checked out, so reads report none and
    `pt tasks` and the board keep working; only writes refuse.
    """
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'project_checkouts'"
    ).fetchone() is not None


def _has_task_id_column(conn: Any) -> bool:
    """True once migration 024 has added project_checkouts.task_id."""
    return any(row[1] == "task_id" for row in conn.execute("PRAGMA table_info(project_checkouts)"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_since(stamp: str | None, now: datetime) -> float | None:
    """Days from ``stamp`` to ``now``; None when it is empty or not ISO 8601.

    Checkout times are UTC with an offset; task times are naive local time,
    which astimezone() reads as this machine's zone.
    """
    try:
        when = datetime.fromisoformat(stamp or "")
    except ValueError:  # governance: allow-silent SF002: checkin_reminders treats an unknown age as old, so it reminds
        return None
    return (now - when.astimezone()).total_seconds() / 86400


def _required(text: str, field: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


class CheckoutsMixin:
    """Checkout operations exposed through the shared DatabaseManager."""

    def get_open_checkouts(self) -> dict[str, dict[str, Any]]:
        """Every project's open checkout, keyed by project id; none before migration 023.

        The columns are the ones migration 023 made, so `pt tasks --json`,
        `pt tasks show --json` and /api/projects keep their shape; only
        checkin_reminders() reads task_id.
        """
        with self._get_conn() as conn:
            if not _has_checkouts_table(conn):
                return {}
            rows = conn.execute(
                f"SELECT {CHECKOUT_COLUMNS} FROM project_checkouts WHERE checked_in_at IS NULL"
            ).fetchall()
        return {row["project_id"]: dict(row) for row in rows}

    def checkout_project(
        self, project_id: str, machine: str, holder: str, note: str | None = None,
        task_id: int | None = None,
    ) -> dict[str, Any]:
        """Open a checkout; raises ProjectCheckedOut naming the current holder.

        ``task_id`` is the card PK it is taken for; None means an owned checkout.
        """
        if machine not in MACHINE_LABELS:
            raise ValueError(f"machine must be one of {', '.join(MACHINE_LABELS)}")
        holder = _required(holder, "holder")
        note = (note or "").strip() or None
        with self._get_conn() as conn:
            if not _has_checkouts_table(conn):
                raise CheckoutsMigrationPendingError()
            has_task_id = _has_task_id_column(conn)
            if task_id is not None and not has_task_id:
                raise CheckoutsMigrationPendingError("024")
            columns = "project_id, machine, holder, note, checked_out_at" + (", task_id" if has_task_id else "")
            values = [project_id, machine, holder, note, _utc_now()] + ([task_id] if has_task_id else [])
            try:
                # One statement, so the project and card checks and the insert
                # share the write lock: a delete can't land between them and
                # leave a checkout for a project or card that is gone.
                cursor = conn.execute(
                    f"INSERT INTO project_checkouts ({columns}) "
                    f"SELECT {', '.join('?' * len(values))} WHERE EXISTS (SELECT 1 FROM projects WHERE id = ?) "
                    "AND (? IS NULL OR EXISTS (SELECT 1 FROM tasks WHERE id = ?))",
                    (*values, project_id, task_id, task_id),
                )
            except sqlite3.IntegrityError:
                # The open-checkout unique index refused it: report who has it.
                held = conn.execute(
                    f"SELECT {CHECKOUT_COLUMNS} FROM project_checkouts WHERE project_id = ? AND checked_in_at IS NULL",
                    (project_id,),
                ).fetchone()
                if held is None:
                    raise  # some other constraint; don't blame a holder
                raise ProjectCheckedOut(dict(held)) from None
            if cursor.rowcount == 0:
                missing = f"no card {task_id}" if task_id is not None else ""
                if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                    missing = f"no project {project_id!r}"
                raise ValueError(missing)
            row = conn.execute(
                f"SELECT {CHECKOUT_COLUMNS} FROM project_checkouts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def checkin_project(
        self, project_id: str, by: str, checkout_id: int | None = None
    ) -> dict[str, Any] | None:
        """Close the matching open checkout; None if no matching checkout is open."""
        by = _required(by, "by")
        with self._get_conn() as conn:
            if not _has_checkouts_table(conn):
                raise CheckoutsMigrationPendingError()
            sql = (
                "UPDATE project_checkouts SET checked_in_at = ?, checked_in_by = ? "
                "WHERE project_id = ? AND checked_in_at IS NULL"
            )
            parameters: list[Any] = [_utc_now(), by, project_id]
            if checkout_id is not None:
                sql += " AND id = ?"
                parameters.append(checkout_id)
            row = conn.execute(f"{sql} RETURNING {CHECKOUT_COLUMNS}", parameters).fetchone()
            conn.commit()
        return dict(row) if row else None

    def checkin_reminders(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Open checkouts taken for a card that are due for check-in, oldest first.

        Each is the checkout row plus ``reason``. Owned checkouts (no task_id)
        never appear; neither does anything before migration 024.
        """
        with self._get_conn() as conn:
            if not _has_checkouts_table(conn) or not _has_task_id_column(conn):
                return []
            rows = conn.execute(
                "SELECT c.*, t.status AS task_status, t.updated_at AS task_updated_at "
                "FROM project_checkouts c LEFT JOIN tasks t ON t.id = c.task_id "
                "WHERE c.checked_in_at IS NULL AND c.task_id IS NOT NULL AND (? IS NULL OR c.project_id = ?) "
                "ORDER BY c.checked_out_at",
                (project_id, project_id),
            ).fetchall()
        now = datetime.now(timezone.utc)
        due = []
        for row in rows:
            status = row["task_status"]
            if status is None:
                reason = "its card no longer exists"
            elif status in ("Done", "Cancelled"):
                reason = f"its card is {status}"
            else:
                out_for = _days_since(row["checked_out_at"], now)
                idle_for = _days_since(row["task_updated_at"], now)
                # An unreadable time counts as old: a missed reminder is the failure this prevents.
                if (out_for is not None and out_for < CHECKIN_REMINDER_DAYS) or (
                        idle_for is not None and idle_for < CHECKIN_REMINDER_DAYS):
                    continue
                reason = f"its card has had no activity for {CHECKIN_REMINDER_DAYS}+ days"
            due.append({**dict(row), "reason": reason})
        return due
