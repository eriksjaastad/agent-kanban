"""Helpers for synthetic test databases only."""
from typing import Any

class LocalTestSupport:
    _SEEDABLE_TASK_COLUMNS = frozenset(
        {"created_at", "updated_at", "completed_at", "archived_at"}
    )

    def seed(self, task_id: int, **columns: Any) -> dict:
        """Set timestamp fields on a synthetic task."""
        unknown = set(columns) - self._SEEDABLE_TASK_COLUMNS
        if unknown:
            raise ValueError(
                f"seed does not set {sorted(unknown)}; "
                f"seedable columns are {sorted(self._SEEDABLE_TASK_COLUMNS)}"
            )
        if not columns:
            return {"task_id": task_id, "updated": 0}

        assignments = ", ".join(f"{name} = ?" for name in columns)
        values = [*columns.values(), task_id]
        conn = self._tracker_conn()
        try:
            cursor = conn.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?", values
            )
            conn.commit()
            return {"task_id": task_id, "updated": cursor.rowcount}
        finally:
            conn.close()

    _COUNTABLE_TABLES = frozenset(
        {
            "tasks", "projects", "task_history", "delete_audit_log", "handoffs",
            "migrations", "calendar_events", "ai_agents", "cron_jobs",
            "services", "ideas", "project_info", "attachments",
            "task_notes_history", "outreach_contacts", "codebase_size_snapshots",
        }
    )

    def count_rows(self, table: str) -> dict:
        """Count rows in one allowlisted table. Tests only.

        The table name is interpolated into the SQL because a table name
        cannot be a bound parameter, which is exactly why it is checked
        against a frozen allowlist first rather than escaped.
        """
        if table not in self._COUNTABLE_TABLES:
            raise ValueError(
                f"{table!r} is not countable; allowed: {sorted(self._COUNTABLE_TABLES)}"
            )
        conn = self._tracker_conn()
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - allowlisted above
            ).fetchone()[0]
        finally:
            conn.close()

    def seed_task(
        self,
        task_id: int,
        text: str,
        project_id: str,
        status: str = "To Do",
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict:
        """Insert a synthetic task with a predictable ID."""
        stamp = created_at or "2026-01-01T00:00:00Z"
        conn = self._tracker_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, text, status, project_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, text, status, project_id, stamp, updated_at or stamp),
            )
            conn.commit()
        finally:
            conn.close()
        return {"task_id": task_id}
