"""Database manager for project tracker operations.

SAFETY POLICY:
==============
All DELETE operations must create a backup BEFORE executing.
This prevents data loss from accidental bulk deletions.
Backups are stored in data/backups/ with timestamps.
"""

import sqlite3
import json
import os
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from .schema import (
    create_database,
    get_db_path,
    mark_if_holding_data,
)
from .pt_id import next_id as pt_next_id
from scripts.utils.validation import (
    BlockedTaskProjectError,
    get_blocked_card_reason,
    validate_acceptance_criteria,
    validate_task_input,
    validate_task_text,
    validate_status,
    validate_priority,
    contains_secret,
)
from scripts.logger import get_logger

logger = get_logger(__name__)


class NotesConflictError(Exception):
    """Compare-and-swap on a task's notes found them changed (#7821)."""


_UNSET: Any = object()

import threading

_LOCAL_SCHEMA_LOCK = threading.Lock()
_local_schema_ensured_paths: set[str] = set()


VALID_STATUS_TRANSITIONS = {
    "Backlog": ["To Do", "Cancelled"],
    "To Do": ["Backlog", "In Progress", "Cancelled"],
    "In Progress": ["To Do", "Backlog", "Review", "Cancelled"],
    "Review": ["In Progress", "To Do", "Backlog", "Done", "Cancelled"],
    "Done": ["Review", "In Progress", "To Do", "Cancelled"],
    "Cancelled": ["Backlog"],
}

def _get_backup_dir(db_path: Path) -> Path:
    """Get or create backup directory."""
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir


class DatabaseManager:
    """Manage all database operations."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the local manager."""
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        self._data_marked = False
        # Run safety checks + schema migrations once per DB path.
        db_key = str(self.db_path.resolve())
        if db_key not in _local_schema_ensured_paths:
            with _LOCAL_SCHEMA_LOCK:
                if db_key not in _local_schema_ensured_paths:
                    create_database(self.db_path)
                    _local_schema_ensured_paths.add(db_key)

    @contextmanager
    def _get_conn(self) -> Generator[Any, None, None]:
        """Get a local SQLite connection context manager."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")  # Enable WAL mode for concurrent access
        conn.row_factory = sqlite3.Row  # Enable dict-like access
        try:
            yield conn
            # The process that commits the first project or task leaves the
            # fresh-database guard's marker, before the file can be replaced.
            if not self._data_marked and conn.total_changes and not conn.in_transaction:
                self._data_marked = mark_if_holding_data(conn, self.db_path)
        finally:
            conn.close()
    
    def _backup_before_delete(
        self,
        table: str,
        where_clause: str,
        params: Tuple,
        reason: str
    ) -> Optional[Path]:
        """Create backup before any DELETE operation.
        
        SAFETY: This method MUST be called before any DELETE FROM statement.
        
        Args:
            table: Table name being deleted from
            where_clause: WHERE clause of the DELETE (e.g., "project_id = ?")
            params: Parameters for the WHERE clause
            reason: Human-readable reason for deletion
            
        Returns:
            Path to backup file if data was backed up, None if no data to backup
        """
        backup_dir = _get_backup_dir(self.db_path)
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            
            # Fetch rows that will be deleted
            query = f"SELECT * FROM {table} WHERE {where_clause}"
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            if not rows:
                return None  # Nothing to backup
            
            # Convert to list of dicts
            data = [dict(row) for row in rows]
            
            # Create timestamped backup
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"{table}_delete_backup_{timestamp}.json"
            
            export_data = {
                "backup_type": "safety_auto_backup",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "table": table,
                "operation": "DELETE",
                "where_clause": where_clause,
                "reason": reason,
                "row_count": len(data),
                "data": data
            }
            
            with open(backup_path, 'w') as f:
                json.dump(export_data, f, indent=2, default=str)
            
            print(f"🛡️  SAFETY: Backup created before delete: {backup_path.name} ({len(data)} rows)")
            return backup_path

    # ==================== ATTACHMENT OPERATIONS (#5216) ====================

    @staticmethod
    def _attachments_dir(task_id: int, *, create: bool = True) -> Path:
        """Return (and create) the storage directory for a task's attachments.

        Delegates to `db.attachment_paths`, which the dashboard also imports.
        Two copies of this path would eventually disagree, and the symptom
        would be attachments that upload fine and cannot be found again.
        """
        from .attachment_paths import attachments_dir

        return attachments_dir(task_id, create=create)

    @classmethod
    def _delete_attachment_files(cls, attachments: List[Dict[str, Any]]) -> None:
        """Trash each attachment's file. See `db.attachment_paths`.

        The implementation moved there when the manager became an RPC proxy:
        it is file work, not row work, and the dashboard needs it too. This
        stays as a thin delegate so existing backend call sites read the same.
        """
        from .attachment_paths import delete_attachment_files

        delete_attachment_files(list(attachments or []))

    def add_attachment(
        self,
        task_id: int,
        filename: str,
        stored_name: str,
        mime_type: Optional[str],
        size_bytes: int,
        uploaded_by: str = "user",
    ) -> Dict[str, Any]:
        """Insert an attachment record and return it."""
        attachment_id = pt_next_id(self.db_path)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO task_attachments
                    (id, task_id, filename, stored_name, mime_type, size_bytes, uploaded_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (attachment_id, task_id, filename, stored_name, mime_type, size_bytes, uploaded_by),
            )
            conn.commit()
            row = cursor.execute(
                "SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            return dict(row)

    def get_attachments(self, task_id: int) -> List[Dict[str, Any]]:
        """Return all attachments for a task, newest first."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY uploaded_at DESC",
                (task_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_attachment(self, task_id: int, stored_name: str) -> Optional[Dict[str, Any]]:
        """Return a single attachment for a task by stored filename."""
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM task_attachments
                WHERE task_id = ? AND stored_name = ?
                """,
                (task_id, stored_name),
            ).fetchone()
            return dict(row) if row else None

    def delete_attachment(self, attachment_id: int, task_id: int) -> Optional[Dict[str, Any]]:
        """Delete an attachment record and return it (so caller can remove the file).

        Verifies task ownership — only deletes if the attachment belongs to task_id.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM task_attachments WHERE id = ? AND task_id = ?",
                (attachment_id, task_id),
            ).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
            conn.commit()
            return dict(row)

    # ==================== PROJECT OPERATIONS ====================

    
    def add_project(
        self,
        project_id: str,
        name: str,
        path: str,
        status: str,
        description: Optional[str] = None,
        phase: Optional[str] = None,
        last_modified: Optional[str] = None,
        completion_pct: int = 0,
        is_infrastructure: bool = False,
        has_index: bool = False,
        index_is_valid: bool = False,
        index_updated_at: Optional[str] = None,
        health_score: Optional[int] = None,
        health_grade: Optional[str] = None,
        project_type: str = 'standard',
    ) -> None:
        """Add or update a project."""
        with self._get_conn() as conn:
            cursor = conn.cursor()

            self._add_project_with_cursor(
                cursor=cursor,
                project_id=project_id,
                name=name,
                path=path,
                status=status,
                description=description,
                phase=phase,
                last_modified=last_modified,
                completion_pct=completion_pct,
                is_infrastructure=is_infrastructure,
                has_index=has_index,
                index_is_valid=index_is_valid,
                index_updated_at=index_updated_at,
                health_score=health_score,
                health_grade=health_grade,
                project_type=project_type,
            )

            conn.commit()

    def _ensure_project_name_available(
        self,
        cursor: sqlite3.Cursor,
        name: str,
        *,
        exclude_project_id: Optional[str] = None,
    ) -> None:
        """Preserve project-name uniqueness in application code.

        The projects table has no DB-enforced UNIQUE(name) constraint (it was
        dropped by migration 006 for cr-sqlite), but the CLI still resolves
        projects by human name. Reject duplicate writes early so name-based
        flows stay unambiguous.

        TOCTOU note: this check is not serializable under concurrent writers in
        WAL mode. Two simultaneous add_project() calls with the same name can
        both pass the SELECT and both succeed. Acceptable for a single-user CLI
        where concurrent writes are rare.
        """
        rows = cursor.execute(
            "SELECT id FROM projects WHERE lower(name) = lower(?)",
            (name,),
        ).fetchall()
        conflicting = [
            (row["id"] if hasattr(row, "keys") else row[0])
            for row in rows
            if (row["id"] if hasattr(row, "keys") else row[0]) != exclude_project_id
        ]
        if conflicting:
            raise ValueError(
                f"Project name '{name}' is already used by project id "
                f"{conflicting[0]!r}. Project names must stay unique."
            )

    def _add_project_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        project_id: str,
        name: str,
        path: str,
        status: str,
        description: Optional[str] = None,
        phase: Optional[str] = None,
        last_modified: Optional[str] = None,
        completion_pct: int = 0,
        is_infrastructure: bool = False,
        has_index: bool = False,
        index_is_valid: bool = False,
        index_updated_at: Optional[str] = None,
        health_score: Optional[int] = None,
        health_grade: Optional[str] = None,
        project_type: str = 'standard',
    ) -> None:
        """Add or update a project using an existing cursor."""
        self._ensure_project_name_available(
            cursor,
            name,
            exclude_project_id=project_id,
        )

        # 🛡️ Preserve created_at, health_score, and health_grade on update
        cursor.execute("SELECT created_at, health_score, health_grade FROM projects WHERE id = ?", (project_id,))
        existing = cursor.fetchone()

        created_at = existing["created_at"] if existing else datetime.now().isoformat()

        # Use provided health data or preserve existing
        final_health_score = health_score if health_score is not None else (existing["health_score"] if existing else None)
        final_health_grade = health_grade if health_grade is not None else (existing["health_grade"] if existing else None)

        # Use ON CONFLICT DO UPDATE rather than INSERT OR REPLACE.
        # INSERT OR REPLACE does an implicit DELETE + INSERT; ON CONFLICT DO UPDATE
        # modifies in-place without touching child rows.
        cursor.execute("""
            INSERT INTO projects
            (id, name, path, status, description, phase, last_modified, created_at, completion_pct,
             is_infrastructure, has_index, index_is_valid, index_updated_at, health_score, health_grade, project_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                path = excluded.path,
                status = excluded.status,
                description = excluded.description,
                phase = excluded.phase,
                last_modified = excluded.last_modified,
                completion_pct = excluded.completion_pct,
                is_infrastructure = excluded.is_infrastructure,
                has_index = excluded.has_index,
                index_is_valid = excluded.index_is_valid,
                index_updated_at = excluded.index_updated_at,
                health_score = excluded.health_score,
                health_grade = excluded.health_grade,
                project_type = excluded.project_type
        """, (project_id, name, path, status, description, phase, last_modified, created_at, completion_pct,
              is_infrastructure, has_index, index_is_valid, index_updated_at, final_health_score, final_health_grade, project_type))
    
    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        """Get a single project by ID."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_projects(self, order_by: str = "last_modified DESC") -> List[Dict[str, Any]]:
        """Get all projects, sorted."""
        # Whitelist allowed order_by values to prevent SQL injection
        allowed_order = {
            "name", "name ASC", "name DESC",
            "status", "status ASC", "status DESC",
            "last_modified", "last_modified ASC", "last_modified DESC",
            "completion_pct", "completion_pct ASC", "completion_pct DESC"
        }
        
        if order_by not in allowed_order:
            raise ValueError(f"Invalid order_by parameter: {order_by}")
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            query = f"SELECT * FROM projects ORDER BY {order_by}"
            cursor.execute(query)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def update_project(self, project_id: str, **kwargs) -> None:
        """Update specific fields of a project."""
        if not kwargs:
            return
        
        # Whitelist allowed fields to prevent SQL injection
        allowed_fields = {
            "name", "path", "status", "phase", "description",
            "completion_pct", "last_modified", "is_infrastructure",
            "has_index", "index_is_valid", "index_updated_at",
            "health_score", "health_grade", "project_type"
        }
        
        # Validate all field names
        for key in kwargs.keys():
            if key not in allowed_fields:
                raise ValueError(f"Invalid field name: {key}")
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if "name" in kwargs:
                self._ensure_project_name_available(
                    cursor,
                    str(kwargs["name"]),
                    exclude_project_id=project_id,
                )
            
            # Build UPDATE query dynamically (now safe - fields are whitelisted)
            fields = ", ".join(f"{key} = ?" for key in kwargs.keys())
            values = list(kwargs.values()) + [project_id]
            
            cursor.execute(f"UPDATE projects SET {fields} WHERE id = ?", values)
            conn.commit()
    
    def _update_health_with_cursor(self, cursor: sqlite3.Cursor, project_id: str, score: int, grade: str) -> None:
        """Update health_score and health_grade using an existing cursor."""
        cursor.execute("""
            UPDATE projects 
            SET health_score = ?, health_grade = ?
            WHERE id = ?
        """, (score, grade, project_id))
    
    def delete_project(self, project_id: str) -> None:
        """Delete a project and all related data."""
        # 🛡️ SAFETY: Backup before delete
        self._backup_before_delete(
            table="projects",
            where_clause="id = ?",
            params=(project_id,),
            reason=f"Deleting project: {project_id}"
        )
        # Also backup related data (cascading deletes)
        self._backup_before_delete("tasks", "project_id = ?", (project_id,), f"Cascade from project delete: {project_id}")
        self._backup_before_delete("cron_jobs", "project_id = ?", (project_id,), f"Cascade from project delete: {project_id}")
        self._backup_before_delete("ai_agents", "project_id = ?", (project_id,), f"Cascade from project delete: {project_id}")
        self._backup_before_delete("service_dependencies", "project_id = ?", (project_id,), f"Cascade from project delete: {project_id}")
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            attachment_rows: List[Dict[str, Any]] = []
            try:
                # Enable delete permission (block_task_delete trigger checks this flag)
                cursor.execute("UPDATE _delete_permissions SET enabled = 1 WHERE id = 1")

                # Explicit child cleanup — FK CASCADE no longer enforced at DB level.
                # Order: deepest children first so no row is left referencing a deleted parent.
                # Some tables (task_attachments, calendar_*) may not exist on every install
                # (lazy-created or not yet migrated), so we check existence first.
                def _tbl_exists(name: str) -> bool:
                    return cursor.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone() is not None

                if _tbl_exists("calendar_event_tasks"):
                    cursor.execute(
                        "DELETE FROM calendar_event_tasks WHERE task_id IN "
                        "(SELECT id FROM tasks WHERE project_id = ?)", (project_id,)
                    )
                    cursor.execute(
                        "DELETE FROM calendar_event_tasks WHERE event_id IN "
                        "(SELECT id FROM calendar_events WHERE project_id = ?)", (project_id,)
                    )
                if _tbl_exists("calendar_events"):
                    # calendar_events keep their rows; project_id set to NULL (was ON DELETE SET NULL)
                    cursor.execute(
                        "UPDATE calendar_events SET project_id = NULL WHERE project_id = ?", (project_id,)
                    )
                if _tbl_exists("task_attachments"):
                    attachment_rows = [
                        dict(row)
                        for row in cursor.execute(
                            """
                            SELECT task_id, stored_name
                            FROM task_attachments
                            WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?)
                            """,
                            (project_id,),
                        ).fetchall()
                    ]
                    cursor.execute(
                        "DELETE FROM task_attachments WHERE task_id IN "
                        "(SELECT id FROM tasks WHERE project_id = ?)", (project_id,)
                    )
                if _tbl_exists("task_history"):
                    cursor.execute("DELETE FROM task_history WHERE project_id = ?", (project_id,))
                cursor.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
                if _tbl_exists("project_info"):
                    cursor.execute("DELETE FROM project_info WHERE project_id = ?", (project_id,))
                if _tbl_exists("project_checkouts"):
                    # Preserve checkout history so primary-key identities cannot be reused.
                    # An open checkout is closed as part of the same delete transaction.
                    cursor.execute(
                        "UPDATE project_checkouts "
                        "SET checked_in_at = ?, checked_in_by = ? "
                        "WHERE project_id = ? AND checked_in_at IS NULL",
                        (datetime.now(timezone.utc).isoformat(), "project deletion", project_id),
                    )
                cursor.execute("DELETE FROM ai_agents WHERE project_id = ?", (project_id,))
                cursor.execute("DELETE FROM service_dependencies WHERE project_id = ?", (project_id,))
                cursor.execute("DELETE FROM cron_jobs WHERE project_id = ?", (project_id,))
                cursor.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                conn.commit()
                self._delete_attachment_files(attachment_rows)
            finally:
                # Always disable delete permission after
                cursor.execute("UPDATE _delete_permissions SET enabled = 0 WHERE id = 1")
                conn.commit()

    # ==================== CRON JOB OPERATIONS ====================
    
    def get_cron_jobs(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get cron jobs, optionally filtered by project."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if project_id:
                cursor.execute("SELECT * FROM cron_jobs WHERE project_id = ?", (project_id,))
            else:
                cursor.execute("SELECT * FROM cron_jobs")
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def _sync_cron_jobs_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        project_id: str,
        cron_jobs: List[Dict[str, Any]],
        allow_deletes: bool = True
    ) -> Dict[str, int]:
        """Sync cron jobs for a project using an existing cursor."""
        existing = cursor.execute(
            "SELECT id, schedule, command, description FROM cron_jobs WHERE project_id = ?",
            (project_id,)
        ).fetchall()

        existing_by_key = {(row["schedule"], row["command"]): row for row in existing}
        desired_keys = set()
        added = updated = deleted = 0

        for job in cron_jobs or []:
            schedule = (job.get("schedule") or "").strip()
            command = (job.get("command") or "").strip()
            if not schedule or not command:
                continue
            description = job.get("description")
            key = (schedule, command)
            desired_keys.add(key)

            existing_row = existing_by_key.get(key)
            if existing_row:
                if existing_row["description"] != description:
                    cursor.execute(
                        "UPDATE cron_jobs SET description = ? WHERE id = ?",
                        (description, existing_row["id"])
                    )
                    updated += 1
            else:
                cursor.execute(
                    "INSERT INTO cron_jobs (project_id, schedule, command, description) VALUES (?, ?, ?, ?)",
                    (project_id, schedule, command, description)
                )
                added += 1

        if allow_deletes:
            delete_ids = [row["id"] for row in existing if (row["schedule"], row["command"]) not in desired_keys]
            if delete_ids:
                # 🛡️ SAFETY: Backup before delete
                self._backup_before_delete(
                    table="cron_jobs",
                    where_clause="project_id = ?",
                    params=(project_id,),
                    reason=f"Sync cron jobs - prune stale ({project_id})"
                )
                cursor.execute(
                    f"DELETE FROM cron_jobs WHERE id IN ({','.join('?' for _ in delete_ids)})",
                    delete_ids
                )
                deleted = cursor.rowcount or 0

        return {"added": added, "updated": updated, "deleted": deleted}
    
    # ==================== AI AGENT OPERATIONS ====================
    
    def add_ai_agent(
        self,
        project_id: str,
        agent_name: str,
        role: Optional[str] = None,
        notes: Optional[str] = None
    ) -> None:
        """Add an AI agent for a project."""
        agent_id = pt_next_id(self.db_path)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ai_agents (id, project_id, agent_name, role, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (agent_id, project_id, agent_name, role, notes))
            conn.commit()
    
    def get_ai_agents(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get AI agents, optionally filtered by project."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if project_id:
                cursor.execute("SELECT * FROM ai_agents WHERE project_id = ?", (project_id,))
            else:
                cursor.execute("SELECT * FROM ai_agents")
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def _sync_ai_agents_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        project_id: str,
        agents: List[Dict[str, Any]],
        allow_deletes: bool = True
    ) -> Dict[str, int]:
        """Sync AI agents for a project using an existing cursor."""
        existing = cursor.execute(
            "SELECT id, agent_name, role, notes FROM ai_agents WHERE project_id = ?",
            (project_id,)
        ).fetchall()

        existing_by_name = {row["agent_name"]: row for row in existing}
        desired_names = set()
        added = updated = deleted = 0

        for agent in agents or []:
            agent_name = (agent.get("agent_name") or "").strip()
            if not agent_name:
                continue
            desired_names.add(agent_name)
            role = agent.get("role")

            existing_row = existing_by_name.get(agent_name)
            if existing_row:
                if existing_row["role"] != role:
                    cursor.execute(
                        "UPDATE ai_agents SET role = ? WHERE id = ?",
                        (role, existing_row["id"])
                    )
                    updated += 1
            else:
                agent_id = pt_next_id(self.db_path)
                cursor.execute(
                    "INSERT INTO ai_agents (id, project_id, agent_name, role) VALUES (?, ?, ?, ?)",
                    (agent_id, project_id, agent_name, role)
                )
                added += 1

        if allow_deletes:
            delete_ids = [row["id"] for row in existing if row["agent_name"] not in desired_names]
            if delete_ids:
                # 🛡️ SAFETY: Backup before delete
                self._backup_before_delete(
                    table="ai_agents",
                    where_clause="project_id = ?",
                    params=(project_id,),
                    reason=f"Sync AI agents - prune stale ({project_id})"
                )
                cursor.execute(
                    f"DELETE FROM ai_agents WHERE id IN ({','.join('?' for _ in delete_ids)})",
                    delete_ids
                )
                deleted = cursor.rowcount or 0

        return {"added": added, "updated": updated, "deleted": deleted}
    
    # ==================== SERVICE OPERATIONS ====================
    
    def add_service(
        self,
        project_id: str,
        service_name: str,
        purpose: Optional[str] = None,
        cost_monthly: Optional[float] = None
    ) -> None:
        """Add a service dependency for a project."""
        service_id = pt_next_id(self.db_path)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO service_dependencies (id, project_id, service_name, purpose, cost_monthly)
                VALUES (?, ?, ?, ?, ?)
            """, (service_id, project_id, service_name, purpose, cost_monthly))
            conn.commit()
    
    def get_services(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get service dependencies, optionally filtered by project."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if project_id:
                cursor.execute("SELECT * FROM service_dependencies WHERE project_id = ?", (project_id,))
            else:
                cursor.execute("SELECT * FROM service_dependencies")
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def _sync_services_with_cursor(
        self,
        cursor: sqlite3.Cursor,
        project_id: str,
        services: List[Dict[str, Any]],
        allow_deletes: bool = True
    ) -> Dict[str, int]:
        """Sync services for a project using an existing cursor."""
        existing = cursor.execute(
            "SELECT id, service_name, purpose, cost_monthly FROM service_dependencies WHERE project_id = ?",
            (project_id,)
        ).fetchall()

        existing_by_name = {row["service_name"]: row for row in existing}
        desired_names = set()
        added = updated = deleted = 0

        for service in services or []:
            service_name = (service.get("service_name") or "").strip()
            if not service_name:
                continue
            desired_names.add(service_name)
            purpose = service.get("purpose")
            cost_monthly = service.get("cost_monthly")

            existing_row = existing_by_name.get(service_name)
            if existing_row:
                if existing_row["purpose"] != purpose or existing_row["cost_monthly"] != cost_monthly:
                    cursor.execute(
                        "UPDATE service_dependencies SET purpose = ?, cost_monthly = ? WHERE id = ?",
                        (purpose, cost_monthly, existing_row["id"])
                    )
                    updated += 1
            else:
                service_id = pt_next_id(self.db_path)
                cursor.execute(
                    "INSERT INTO service_dependencies (id, project_id, service_name, purpose, cost_monthly) VALUES (?, ?, ?, ?, ?)",
                    (service_id, project_id, service_name, purpose, cost_monthly)
                )
                added += 1

        if allow_deletes:
            delete_ids = [row["id"] for row in existing if row["service_name"] not in desired_names]
            if delete_ids:
                # 🛡️ SAFETY: Backup before delete
                self._backup_before_delete(
                    table="service_dependencies",
                    where_clause="project_id = ?",
                    params=(project_id,),
                    reason=f"Sync services - prune stale ({project_id})"
                )
                cursor.execute(
                    f"DELETE FROM service_dependencies WHERE id IN ({','.join('?' for _ in delete_ids)})",
                    delete_ids
                )
                deleted = cursor.rowcount or 0

        return {"added": added, "updated": updated, "deleted": deleted}
    
    # ==================== TASK OPERATIONS ====================
    
    def add_task(
        self,
        text: str,
        project_id: str,
        status: str = "Backlog",
        priority: Optional[str] = None,
        prompt: Optional[str] = None,
        task_type: Optional[str] = None,
        category: Optional[str] = None,
        review_comment: Optional[str] = None,
        notes: Optional[str] = None,
        parent_id: Optional[int] = None,
        blocked_by: Optional[Any] = None,
        sequence_order: Optional[int] = None,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new task — the low-level DB primitive.

        This is the permissive primitive used internally (test fixtures,
        migrations, maintenance scripts) and does NOT require acceptance
        criteria in ``notes``. Every user/agent-facing card-creation surface
        (``pt tasks create``, ``POST /api/tasks``, the ``kanban_add_task``
        MCP tool) must go through ``create_card()`` instead, which enforces
        acceptance criteria (#7608) before delegating here. See
        ``validate_acceptance_criteria()`` in scripts/utils/validation.py
        for the full rationale on why that check lives at the call sites
        rather than inside this method.

        Args:
            text: Task description (1-1000 characters)
            project_id: Project identifier
            status: Task status (default: "Backlog")
            priority: Task priority (optional)
            prompt: Structured execution instructions for agents (optional)
            task_type: Task type (manual or agent)
            review_comment: Reviewer feedback when task is sent back (optional)
            parent_id: Parent task ID for subtasks (optional)
            blocked_by: JSON string or list of blocking task IDs (optional)
            sequence_order: Ordering hint for subtasks (optional)
            created_by: Short origin label (e.g. "project-tracker", "architect") — who/where filed this card

        Returns:
            Task dictionary with all fields including generated id and timestamps

        Raises:
            ValueError: If validation fails (invalid input, secret detected, etc.)
        """
        effective_task_type = task_type or ("agent" if prompt and prompt.strip() else "manual")

        # Comprehensive validation including secret detection
        is_valid, error_message = validate_task_input(
            text=text,
            project_id=project_id,
            status=status,
            priority=priority,
            task_type=effective_task_type,
            db_manager=self
        )
        if not is_valid:
            blocked_reason = get_blocked_card_reason(project_id)
            if blocked_reason and error_message == blocked_reason:
                raise BlockedTaskProjectError(error_message)
            raise ValueError(error_message)
        
        blocked_by_value = None
        if blocked_by is not None:
            if isinstance(blocked_by, str):
                blocked_by_value = blocked_by
            elif isinstance(blocked_by, list):
                blocked_by_value = json.dumps(blocked_by)
            else:
                raise ValueError("blocked_by must be a JSON string or list of task IDs")
        
        now = datetime.now().isoformat()
        task_id = pt_next_id(self.db_path)
        history_id = pt_next_id(self.db_path)
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            effective_category = (category.strip() or None) if category is not None else None
            
            # Text is stored as typed; HTML renderers escape it (#8246).
            cursor.execute("""
                INSERT INTO tasks (
                    id,
                    text,
                    status,
                    project_id,
                    priority,
                    created_at,
                    updated_at,
                    completed_at,
                    prompt,
                    task_type,
                    category,
                    review_comment,
                    notes,
                    parent_id,
                    blocked_by,
                    sequence_order,
                    created_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_id,
                text,
                status,
                project_id,
                priority,
                now,
                now,
                None if status != "Done" else now,
                prompt,
                effective_task_type,
                effective_category,
                review_comment,
                notes,
                parent_id,
                blocked_by_value,
                sequence_order,
                created_by,
            ))

            # Record history entry for creation
            cursor.execute("""
                INSERT INTO task_history (id, task_id, project_id, event_type, old_status, new_status, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (history_id, task_id, project_id, "created", None, status, now))

            # Assign a sequential display_id for human-readable card numbers.
            # task_display_ids is LOCAL_ONLY — never synced — so MAX+1 is safe.
            cursor.execute(
                "INSERT OR IGNORE INTO task_display_ids (task_id, display_id) "
                "VALUES (?, COALESCE((SELECT MAX(display_id) FROM task_display_ids), 0) + 1)",
                (task_id,),
            )

            conn.commit()

            # Return the created task
            return self.get_task(task_id)

    def create_card(self, **kwargs) -> Dict[str, Any]:
        """Create a new Kanban card — the enforcing entry point (#7608).

        Every user/agent-facing card-creation surface (``pt tasks create``,
        ``POST /api/tasks``, the ``kanban_add_task`` MCP tool) calls this
        instead of ``add_task()`` directly. It requires at least one
        concrete acceptance-criterion checklist line in ``notes`` — see
        ``validate_acceptance_criteria()`` — then delegates to ``add_task()``
        for everything else. No task_type is exempt, including "proposal";
        subtasks (parent_id set) are not exempt either.

        Accepts the exact same keyword arguments as ``add_task()`` (notes
        is required in practice, though still a keyword for signature
        symmetry) and returns the same task dictionary.

        Raises:
            ValueError: If acceptance criteria are missing/malformed, or for
                any reason add_task() itself would raise.
        """
        is_valid, error_message = validate_acceptance_criteria(kwargs.get("notes"))
        if not is_valid:
            raise ValueError(error_message)
        return self.add_task(**kwargs)

    def get_task_counts_by_project(
        self,
        statuses: Optional[List[str]] = None,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """Count open cards grouped by (project, status).

        The dashboard's board breakdown needs one number per project per
        column, not the rows themselves. Fetching 2,400 enriched tasks to
        length() them client-side is the N+1 the board already learned to
        avoid, so this aggregates in SQLite and returns at most
        projects x statuses rows.

        Args:
            statuses: Restrict to these statuses (optional; all when None).
            include_archived: Include tasks with a non-NULL archived_at.
                Archived cards are Done rows past the per-project display
                cap, so the default False matches what the board renders.

        Returns:
            List of {"project_id", "status", "count"} dicts, unordered.
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()

            query = "SELECT project_id, status, COUNT(*) AS count FROM tasks WHERE 1=1"
            params: List[Any] = []

            if not include_archived:
                query += " AND archived_at IS NULL"

            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                query += f" AND status IN ({placeholders})"
                params.extend(statuses)

            query += " GROUP BY project_id, status"

            cursor.execute(query, params)
            return [
                {
                    "project_id": row["project_id"],
                    "status": row["status"],
                    "count": row["count"],
                }
                for row in cursor.fetchall()
            ]

    def get_tasks(
        self,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get tasks with optional filtering.

        Args:
            project_id: Filter by project ID (optional)
            status: Filter by status (optional)
            include_archived: Include tasks with a non-NULL archived_at.
                Archived tasks are Done cards past the per-project display
                cap — they still exist and are never deleted, they are just
                hidden from the board by default. Pass True for backups,
                exports, and any count that must match a delete operation.

        Returns:
            List of task dictionaries
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM tasks WHERE 1=1"
            params = []

            if not include_archived:
                query += " AND archived_at IS NULL"

            if project_id:
                query += " AND project_id = ?"
                params.append(project_id)

            if status:
                query += " AND status = ?"
                params.append(status)

            query += " ORDER BY CASE priority WHEN 'Critical' THEN 0 WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END, created_at DESC"

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Get a single task by ID.
        
        Args:
            task_id: Task ID
            
        Returns:
            Task dictionary or None if not found
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def resolve_task_id(self, token: int) -> Optional[int]:
        """Resolve a display_id or Snowflake task_id to the canonical task_id.

        If token < 10^12 (legacy-range or display_id), first checks
        task_display_ids for a display_id match; falls back to tasks.id.
        If token >= 10^12, it is a Snowflake PK — return as-is if it exists.
        """
        _SNOWFLAKE_THRESHOLD = 10 ** 12
        with self._get_conn() as conn:
            if token < _SNOWFLAKE_THRESHOLD:
                row = conn.execute(
                    "SELECT task_id FROM task_display_ids WHERE display_id = ?", (token,)
                ).fetchone()
                if row:
                    return row[0]
            row = conn.execute(
                "SELECT id FROM tasks WHERE id = ?", (token,)
            ).fetchone()
            return row[0] if row else None

    def get_done_since(self, since: str) -> List[Dict[str, Any]]:
        """(project_id, completed_at) of every card marked Done at or after ``since``, archived ones included."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT project_id, completed_at FROM tasks"
                " WHERE status = 'Done' AND completed_at >= ?",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_owner_cards(self, statuses: List[str], prefix: str,
                        include_archived: bool = False) -> List[Dict[str, Any]]:
        """Open cards waiting on the owner, oldest first.

        The board's rule: a card that needs the owner starts with ``prefix``
        (config.owner_prefix). Same filter as ``pt tasks`` (not archived, no
        proposals), so the counts agree. ``include_archived`` adds the Done cards
        trimmed off the board, for history that must see every such card ever opened.
        """
        placeholders = ",".join("?" for _ in statuses)
        archived = "" if include_archived else " AND t.archived_at IS NULL"
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT t.project_id, t.text, t.status, t.created_at, d.display_id"
                " FROM tasks t LEFT JOIN task_display_ids d ON d.task_id = t.id"
                f" WHERE t.status IN ({placeholders}){archived}"
                " AND substr(t.text, 1, ?) = ?"
                " AND COALESCE(t.task_type, '') != 'proposal'"
                " ORDER BY t.created_at",
                [*statuses, len(prefix), prefix],
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task_display_id(self, task_id: int) -> Optional[int]:
        """Return the display_id for a given Snowflake task_id, or None."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT display_id FROM task_display_ids WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row[0] if row else None

    def get_task_display_id_map(self, task_ids: list) -> dict:
        """Return a {task_id: display_id} mapping for all given task_ids in one query."""
        if not task_ids:
            return {}
        placeholders = ",".join("?" * len(task_ids))
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT task_id, display_id FROM task_display_ids WHERE task_id IN ({placeholders})",
                task_ids,
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def update_task(
        self,
        task_id: int,
        notes_source: Optional[str] = None,
        expected_notes: Any = _UNSET,
        **updates
    ) -> Dict[str, Any]:
        """Update task fields.
        
        Args:
            task_id: Task ID
            notes_source: Origin label for a task_notes_history row written by
                this call ("cli", "api", ...). Not a column — never stored on
                tasks. Defaults to "unknown" when a notes change is recorded.
            expected_notes: Optional compare-and-swap guard (#7821). When
                passed (str or None), the UPDATE only applies if the stored
                notes still equal this value; otherwise nothing is written
                (the notes-history row rolls back with it) and
                NotesConflictError is raised.
            **updates: Fields to update (text, status, priority)
            
        Returns:
            Updated task dictionary
            
        Raises:
            ValueError: If task not found or invalid field values
        """
        if not updates:
            return self.get_task(task_id)
        
        # Whitelist allowed fields
        allowed_fields = {
            "text",
            "status",
            "priority",
            "prompt",
            "task_type",
            "review_comment",
            "title",
            "notes",
            "commit_sha",
            "category",
            "parent_id",
            "blocked_by",
            "sequence_order",
            "machine",
            "project_id",
        }
        for key in updates.keys():
            if key not in allowed_fields:
                raise ValueError(f"Invalid field name: {key}. Allowed: {allowed_fields}")
        
        # Get existing task
        existing_task = self.get_task(task_id)
        if not existing_task:
            raise ValueError(f"Task with ID {task_id} not found")

        # Auto-promote to agent when a prompt is added and task_type is not explicit
        if "prompt" in updates and "task_type" not in updates:
            prompt_value = updates.get("prompt")
            if isinstance(prompt_value, str) and prompt_value.strip():
                updates["task_type"] = "agent"
        
        # Validate text if provided (includes secret detection). Text is stored
        # as typed; whatever renders it as HTML escapes it there (#8246).
        if "text" in updates:
            text = updates["text"]
            # Validate text length
            is_valid, error_message = validate_task_text(text)
            if not is_valid:
                raise ValueError(error_message)
            # Check for secrets
            is_secret, pattern = contains_secret(text)
            if is_secret:
                raise ValueError(f"Task text contains potential secret pattern: {pattern}")
        
        # Normalize blocked_by if provided
        if "blocked_by" in updates and isinstance(updates["blocked_by"], list):
            updates["blocked_by"] = json.dumps(updates["blocked_by"])

        # Validate status if provided
        if "status" in updates:
            is_valid, error_message = validate_status(updates["status"])
            if not is_valid:
                raise ValueError(error_message)
        
        # Validate priority if provided
        if "priority" in updates:
            is_valid, error_message = validate_priority(updates["priority"])
            if not is_valid:
                raise ValueError(error_message)

        # Validate task type if provided
        if "task_type" in updates:
            from scripts.utils.validation import validate_task_type
            is_valid, error_message = validate_task_type(updates["task_type"])
            if not is_valid:
                raise ValueError(error_message)

        now = datetime.now().isoformat()
        old_status = existing_task["status"]
        new_status = updates.get("status", old_status)

        if "review_comment" in updates and updates["review_comment"] is not None:
            updates["review_comment"] = updates["review_comment"].strip() or None

        if new_status != old_status:
            allowed = VALID_STATUS_TRANSITIONS.get(old_status, [])
            if new_status not in allowed:
                raise ValueError(
                    f"Cannot move from {old_status} to {new_status}. "
                    f"Valid next: {', '.join(allowed) if allowed else 'none'}."
                )

        if old_status == "Review" and new_status != "Review":
            if updates.get("review_comment") is None and not existing_task.get("review_comment"):
                updates["review_comment"] = "Returned from Review"

        # Log when agent task starts without a prompt
        if new_status == "In Progress":
            prompt_value = updates.get("prompt")
            if prompt_value is None:
                prompt_value = existing_task.get("prompt")
            task_type_value = updates.get("task_type")
            if task_type_value is None:
                task_type_value = existing_task.get("task_type") or "manual"
            if task_type_value == "agent" and not prompt_value:
                logger.warning(f"Agent task #{task_id} started without a prompt")
        
        max_retries = 5
        for attempt in range(max_retries):
            locked = False
            with self._get_conn() as conn:
                try:
                    cursor = conn.cursor()

                    # Record a notes-history row in the same transaction when
                    # notes actually change. The dashboard sends notes on every
                    # save, so treating None and "" as equal keeps those no-ops
                    # quiet; a real change writes one evidence row.
                    if "notes" in updates:
                        notes_row = cursor.execute(
                            "SELECT notes FROM tasks WHERE id = ?", (task_id,)
                        ).fetchone()
                        old_notes = (
                            notes_row["notes"] if notes_row else existing_task.get("notes")
                        )
                        new_notes = updates["notes"]
                        old_notes_norm = "" if old_notes is None else old_notes
                        new_notes_norm = "" if new_notes is None else new_notes
                        if old_notes_norm != new_notes_norm:
                            cursor.execute("""
                                INSERT INTO task_notes_history
                                    (task_id, project_id, old_notes, new_notes, source, timestamp)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (
                                task_id,
                                existing_task["project_id"],
                                old_notes,
                                new_notes,
                                notes_source or "unknown",
                                now,
                            ))

                    # Build UPDATE query
                    fields = []
                    values = []
                    
                    for key, value in updates.items():
                        fields.append(f"{key} = ?")
                        values.append(value)
                    
                    # Always update updated_at
                    fields.append("updated_at = ?")
                    values.append(now)
                    
                    # Set completed_at if moving to Done
                    if new_status == "Done" and old_status != "Done":
                        fields.append("completed_at = ?")
                        values.append(now)
                    elif new_status != "Done" and old_status == "Done":
                        # Clear completed_at if moving away from Done
                        fields.append("completed_at = ?")
                        values.append(None)
                        # Un-archive too. archived_at is only meaningful for
                        # Done cards — it's a display cap on finished work.
                        # A reopened card that kept the flag would sit in an
                        # active status and still be filtered out of every
                        # default board query, i.e. vanish entirely (#6870).
                        fields.append("archived_at = ?")
                        values.append(None)

                    values.append(task_id)
                    
                    query = f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?"
                    if expected_notes is not _UNSET:
                        query += " AND notes IS ?"
                        values.append(expected_notes)
                    cursor.execute(query, values)
                    if expected_notes is not _UNSET and cursor.rowcount != 1:
                        raise NotesConflictError(
                            f"Notes for task {task_id} changed since they were read"
                        )
                    
                    # Record history entry if status changed
                    if old_status != new_status:
                        event_type = "completed" if new_status == "Done" else "status_changed"
                        history_id = pt_next_id(self.db_path)
                        cursor.execute("""
                            INSERT INTO task_history (id, task_id, project_id, event_type, old_status, new_status, timestamp)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (history_id, task_id, existing_task["project_id"], event_type, old_status, new_status, now))
                    
                    conn.commit()

                    cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
                    row = cursor.fetchone()
                    return dict(row) if row else None
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                        locked = True
                        try:
                            conn.rollback()
                        except sqlite3.Error:  # governance: allow-silent SF001: rollback of a locked attempt before retrying; the retry reruns the write and the last attempt raises
                            pass
                    else:
                        raise
                except Exception:
                    try:
                        conn.rollback()
                    except sqlite3.Error:  # governance: allow-silent SF001: rollback while the original exception is re-raised on the next line
                        pass
                    raise
            
            if locked:
                sleep_time = (2 ** attempt) * 0.01
                time.sleep(sleep_time)
                continue

            # Return updated task (already returned inside loop)
            return self.get_task(task_id)

    def get_task_notes_history(self, task_id: int) -> List[Dict[str, Any]]:
        """Return notes-history rows for a task, newest first.

        Rows survive task deletion on purpose: they are evidence of what a
        card's notes used to say, not children that should follow the card
        into the bin.
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, task_id, project_id, old_notes, new_notes, source, timestamp
                FROM task_notes_history
                WHERE task_id = ?
                ORDER BY timestamp DESC, id DESC
            """, (task_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_subtasks(
        self, parent_id: int, include_archived: bool = False
    ) -> List[Dict[str, Any]]:
        """Get all subtasks of a parent task.

        Args:
            parent_id: Parent task ID
            include_archived: Include subtasks with a non-NULL archived_at.
                Same contract as ``get_tasks``: archived cards are Done cards
                past the per-project display cap — still present, just hidden
                from the board by default. Leaving them in inflates progress
                denominators, so the default is to filter them out.

        Returns:
            List of subtask dictionaries
        """
        query = "SELECT * FROM tasks WHERE parent_id = ?"
        if not include_archived:
            query += " AND archived_at IS NULL"
        query += " ORDER BY sequence_order ASC, created_at ASC"
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (parent_id,))
            return [dict(row) for row in cursor.fetchall()]

    def get_subtask_progress(
        self, parent_id: int, include_archived: bool = False
    ) -> Dict[str, Any]:
        """Get completion progress for a parent task's subtasks.

        ``include_archived`` is passed straight through to ``get_subtasks``;
        without it the flag would be unreachable from the real call sites.
        """
        subtasks = self.get_subtasks(parent_id, include_archived=include_archived)
        total = len(subtasks)
        done = len([t for t in subtasks if t["status"] == "Done"])
        return {
            "total": total,
            "done": done,
            "percent": int(done / total * 100) if total > 0 else 0
        }

    @staticmethod
    def parse_blocked_by(task: Dict[str, Any]) -> Tuple[List[Any], Optional[str]]:
        """Split a task's stored blocked_by into (ids, error).

        ``error`` is a human reason when the stored value is not a JSON list,
        e.g. ``blocked_by unreadable: '7,8'``. Callers must treat an error as
        BLOCKED (#6900): an unreadable dependency is not proof of "unblocked".
        """
        raw = task.get("blocked_by")
        if not raw:
            return [], None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return [], f"blocked_by unreadable: {raw!r}"
        if not isinstance(parsed, list):
            return [], f"blocked_by unreadable: {raw!r}"
        return parsed, None

    def blocked_by_error(self, task_id: int) -> Optional[str]:
        """Reason this task's blocked_by cannot be read, or None if it can.

        ``is_blocked`` answers ``(True, [])`` for such a task; this names why.
        """
        task = self.get_task(task_id)
        if not task:
            return None
        return self.parse_blocked_by(task)[1]

    def get_blocking_tasks(self, task_id: int) -> List[Dict[str, Any]]:
        """Get tasks that are blocking this task from starting.

        An unreadable blocked_by yields no tasks here; ``is_blocked`` still
        reports the task as blocked and ``blocked_by_error`` names the reason.
        """
        task = self.get_task(task_id)
        if not task or not task.get("blocked_by"):
            return []

        blocked_by_ids, error = self.parse_blocked_by(task)
        if error:
            logger.warning("Task #%s: %s", task_id, error)
            return []

        results = []
        for tid in blocked_by_ids:
            blocking_task = self.get_task(tid)
            if blocking_task:
                results.append(blocking_task)
        return results

    def get_blocked_tasks(
        self, task_id: int, include_archived: bool = False
    ) -> List[Dict[str, Any]]:
        """Get tasks that are blocked by this task (reverse lookup).

        Args:
            task_id: The blocking task's ID
            include_archived: Include tasks with a non-NULL archived_at.
                Same contract as ``get_tasks`` — archived cards are hidden
                from the board by default, so they are filtered out here too.

        Returns:
            List of task dictionaries that name ``task_id`` in blocked_by
        """
        query = "SELECT * FROM tasks WHERE blocked_by IS NOT NULL"
        if not include_archived:
            query += " AND archived_at IS NULL"
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(query)

            results = []
            for row in cursor.fetchall():
                task = dict(row)
                if task.get("blocked_by"):
                    try:
                        blocked_by = json.loads(task["blocked_by"])
                        if task_id in blocked_by:
                            results.append(task)
                    except (json.JSONDecodeError, TypeError):
                        continue
            return results

    def is_blocked(self, task_id: int) -> Tuple[bool, List[int]]:
        """Check if task is blocked by incomplete tasks.

        Fails closed: an unreadable blocked_by returns ``(True, [])`` (blocked,
        no resolvable blocker ids); ``blocked_by_error`` gives the reason.
        """
        task = self.get_task(task_id)
        if task and self.parse_blocked_by(task)[1]:
            return (True, [])
        blocking = self.get_blocking_tasks(task_id)
        incomplete = [t["id"] for t in blocking if t["status"] != "Done"]
        return (len(incomplete) > 0, incomplete)
    
    def delete_task(self, task_id: int) -> None:
        """Delete a task.

        Args:
            task_id: Task ID

        Raises:
            ValueError: If task not found
        """
        # Verify task exists
        if not self.get_task(task_id):
            raise ValueError(f"Task with ID {task_id} not found")

        self._ensure_delete_allowed(operation=f"delete_task:{task_id}")

        # 🛡️ SAFETY: Backup before delete
        self._backup_before_delete(
            table="tasks",
            where_clause="id = ?",
            params=(task_id,),
            reason=f"Deleting task: {task_id}"
        )

        with self._get_conn() as conn:
            cursor = conn.cursor()
            attachment_rows: List[Dict[str, Any]] = []
            try:
                # Collect all descendant task IDs before enabling delete permission
                cursor.execute("""
                    WITH RECURSIVE descendants AS (
                        SELECT id FROM tasks WHERE parent_id = ?
                        UNION ALL
                        SELECT t.id FROM tasks t
                        INNER JOIN descendants d ON t.parent_id = d.id
                    )
                    SELECT id FROM descendants
                """, (task_id,))
                descendant_ids = [r[0] for r in cursor.fetchall()]

                # Enable delete permission (block_task_delete trigger checks this flag)
                cursor.execute("UPDATE _delete_permissions SET enabled = 1 WHERE id = 1")

                # Explicit child cleanup — FK CASCADE no longer enforced at DB level.
                # Guard optional tables that may not exist on every install.
                def _tbl_exists_task(name: str) -> bool:
                    return cursor.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                    ).fetchone() is not None

                if descendant_ids:
                    placeholders = ",".join("?" for _ in descendant_ids)
                    if _tbl_exists_task("calendar_event_tasks"):
                        cursor.execute(
                            f"DELETE FROM calendar_event_tasks WHERE task_id IN ({placeholders})",
                            descendant_ids,
                        )
                    if _tbl_exists_task("task_attachments"):
                        attachment_rows.extend(
                            dict(row)
                            for row in cursor.execute(
                                f"SELECT task_id, stored_name FROM task_attachments WHERE task_id IN ({placeholders})",
                                descendant_ids,
                            ).fetchall()
                        )
                        cursor.execute(
                            f"DELETE FROM task_attachments WHERE task_id IN ({placeholders})",
                            descendant_ids,
                        )
                    cursor.execute(
                        f"DELETE FROM task_history WHERE task_id IN ({placeholders})",
                        descendant_ids,
                    )
                    cursor.execute(
                        f"DELETE FROM tasks WHERE id IN ({placeholders})",
                        descendant_ids,
                    )

                if _tbl_exists_task("calendar_event_tasks"):
                    cursor.execute("DELETE FROM calendar_event_tasks WHERE task_id = ?", (task_id,))
                if _tbl_exists_task("task_attachments"):
                    attachment_rows.extend(
                        dict(row)
                        for row in cursor.execute(
                            "SELECT task_id, stored_name FROM task_attachments WHERE task_id = ?",
                            (task_id,),
                        ).fetchall()
                    )
                    cursor.execute("DELETE FROM task_attachments WHERE task_id = ?", (task_id,))
                cursor.execute("DELETE FROM task_history WHERE task_id = ?", (task_id,))
                cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                conn.commit()
                self._delete_attachment_files(attachment_rows)
            finally:
                # Always disable delete permission after
                cursor.execute("UPDATE _delete_permissions SET enabled = 0 WHERE id = 1")
                conn.commit()

    def delete_done_tasks(self, project_id: Optional[str] = None) -> int:
        """Delete all tasks in Done status.

        Args:
            project_id: Filter by project ID (optional). If None, deletes Done tasks from all projects.

        Returns:
            Number of tasks deleted
        """
        if os.getenv("ALLOW_BULK_DELETE", "0") != "1":
            raise ValueError("Bulk delete is disabled")
        self._ensure_delete_allowed(operation="delete_done_tasks")

        # 🛡️ SAFETY: Backup before bulk delete
        if project_id:
            self._backup_before_delete(
                table="tasks",
                where_clause="status = 'Done' AND project_id = ?",
                params=(project_id,),
                reason=f"Bulk delete Done tasks for project: {project_id}"
            )
        else:
            self._backup_before_delete(
                table="tasks",
                where_clause="status = 'Done'",
                params=(),
                reason="Bulk delete ALL Done tasks across all projects"
            )
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            attachment_rows: List[Dict[str, Any]] = []
            try:
                # Collect task IDs to be deleted so we can clean child rows first
                if project_id:
                    cursor.execute(
                        "SELECT id FROM tasks WHERE status = 'Done' AND project_id = ?", (project_id,)
                    )
                else:
                    cursor.execute("SELECT id FROM tasks WHERE status = 'Done'")
                done_ids = [r[0] for r in cursor.fetchall()]

                # Enable delete permission (block_task_delete trigger checks this flag)
                cursor.execute("UPDATE _delete_permissions SET enabled = 1 WHERE id = 1")

                # Explicit child cleanup — FK CASCADE no longer enforced at DB level.
                if done_ids:
                    placeholders = ",".join("?" for _ in done_ids)
                    has_cet = cursor.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calendar_event_tasks'"
                    ).fetchone() is not None
                    has_ta = cursor.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_attachments'"
                    ).fetchone() is not None
                    if has_cet:
                        cursor.execute(
                            f"DELETE FROM calendar_event_tasks WHERE task_id IN ({placeholders})",
                            done_ids,
                        )
                    if has_ta:
                        attachment_rows = [
                            dict(row)
                            for row in cursor.execute(
                                f"SELECT task_id, stored_name FROM task_attachments WHERE task_id IN ({placeholders})",
                                done_ids,
                            ).fetchall()
                        ]
                        cursor.execute(
                            f"DELETE FROM task_attachments WHERE task_id IN ({placeholders})",
                            done_ids,
                        )
                    cursor.execute(
                        f"DELETE FROM task_history WHERE task_id IN ({placeholders})",
                        done_ids,
                    )

                if project_id:
                    cursor.execute("DELETE FROM tasks WHERE status = 'Done' AND project_id = ?", (project_id,))
                else:
                    cursor.execute("DELETE FROM tasks WHERE status = 'Done'")
                deleted_count = cursor.rowcount
                conn.commit()
                self._delete_attachment_files(attachment_rows)
                return deleted_count
            finally:
                # Always disable delete permission after
                cursor.execute("UPDATE _delete_permissions SET enabled = 0 WHERE id = 1")
                conn.commit()

    def archive_done_tasks(self, keep_per_project: int = 25) -> int:
        """Hide older Done cards from the board without deleting anything.

        Sets `archived_at` on Done cards beyond the newest `keep_per_project`
        **per project** (ordered by COALESCE(completed_at, updated_at) DESC).
        Cards that are already archived don't count against the cap — the cap
        is on what the board shows, not on what exists.

        This never deletes a row and never touches `status` or `completed_at`.
        It replaces the old portfolio-wide `trim_done_tasks` hard delete, which
        let a burst of completions in one project evict another project's
        history (1,288 Done cards were destroyed that way; #6870).

        Returns:
            Number of tasks newly archived
        """
        if keep_per_project < 0:
            raise ValueError("keep_per_project must be >= 0")

        archived_at = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE tasks
                SET archived_at = ?
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY project_id
                            ORDER BY COALESCE(completed_at, updated_at) DESC, id DESC
                        ) AS rn
                        FROM tasks
                        WHERE status = 'Done' AND archived_at IS NULL
                    )
                    WHERE rn > ?
                )
                """,
                (archived_at, keep_per_project),
            )
            archived = cursor.rowcount
            conn.commit()
            return archived

    def trim_done_tasks(self, keep: int = 75) -> int:
        """Delete oldest Done tasks beyond the retention cap.

        DEPRECATED — destructive, and portfolio-wide rather than per-project.
        Nothing calls this automatically any more; `pt tasks done` uses
        `archive_done_tasks()` instead (#6870). Retained only so existing
        callers/tests keep working.

        Keeps the most recent `keep` Done tasks (by completed_at/updated_at),
        deletes the rest. Runs backup before deletion.

        Returns:
            Number of tasks trimmed
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # Count how many Done tasks exist
            cursor.execute("SELECT COUNT(*) FROM tasks WHERE status = 'Done'")
            total_done = cursor.fetchone()[0]
            if total_done <= keep:
                return 0

            to_delete = total_done - keep
            # Backup before trimming
            self._backup_before_delete(
                table="tasks",
                where_clause="status = 'Done'",
                params=(),
                reason=f"Trim Done tasks: keeping {keep}, deleting {to_delete}"
            )

            try:
                # Collect the specific task IDs to trim so we can clean child rows first
                cursor.execute("""
                    SELECT id FROM tasks
                    WHERE status = 'Done'
                    ORDER BY COALESCE(completed_at, updated_at) ASC
                    LIMIT ?
                """, (to_delete,))
                trim_ids = [r[0] for r in cursor.fetchall()]
                attachment_rows: List[Dict[str, Any]] = []

                cursor.execute("UPDATE _delete_permissions SET enabled = 1 WHERE id = 1")

                # Explicit child cleanup — FK CASCADE no longer enforced at DB level.
                if trim_ids:
                    placeholders = ",".join("?" for _ in trim_ids)
                    has_cet = cursor.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='calendar_event_tasks'"
                    ).fetchone() is not None
                    has_ta = cursor.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_attachments'"
                    ).fetchone() is not None
                    if has_cet:
                        cursor.execute(
                            f"DELETE FROM calendar_event_tasks WHERE task_id IN ({placeholders})",
                            trim_ids,
                        )
                    if has_ta:
                        attachment_rows = [
                            dict(row)
                            for row in cursor.execute(
                                f"SELECT task_id, stored_name FROM task_attachments WHERE task_id IN ({placeholders})",
                                trim_ids,
                            ).fetchall()
                        ]
                        cursor.execute(
                            f"DELETE FROM task_attachments WHERE task_id IN ({placeholders})",
                            trim_ids,
                        )
                    cursor.execute(
                        f"DELETE FROM task_history WHERE task_id IN ({placeholders})",
                        trim_ids,
                    )

                cursor.execute(
                    f"DELETE FROM tasks WHERE id IN ({','.join('?' for _ in trim_ids)})",
                    trim_ids,
                )
                trimmed = cursor.rowcount
                conn.commit()
                self._delete_attachment_files(attachment_rows)
                return trimmed
            finally:
                cursor.execute("UPDATE _delete_permissions SET enabled = 0 WHERE id = 1")
                conn.commit()

    def raw_import_tasks(self, tasks: list[dict[str, Any]]) -> int:
        """Raw import tasks, preserving IDs. Use for disaster recovery."""
        success_count = 0
        with self._get_conn() as conn:
            cursor = conn.cursor()
            for task in tasks:
                try:
                    # Check if project exists
                    cursor.execute("SELECT 1 FROM projects WHERE id = ?", (task["project_id"],))
                    if not cursor.fetchone():
                        print(f"  ⚠️ Skipping task #{task['id']}: Project '{task['project_id']}' not found")
                        continue
                        
                    cursor.execute("""
                        INSERT OR REPLACE INTO tasks 
                        (id, text, status, project_id, priority, created_at, updated_at, completed_at, prompt, task_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        task["id"], task["text"], task["status"], task["project_id"],
                        task.get("priority"), task["created_at"], task["updated_at"],
                        task.get("completed_at"), task.get("prompt"),
                        task.get("task_type") or "manual"
                    ))
                    success_count += 1
                except Exception as e:
                    print(f"  ❌ Failed to import task #{task.get('id')}: {e}")
            conn.commit()
        return success_count

    def _ensure_delete_allowed(self, operation: str) -> None:
        """Enforce SAFE_MODE before any delete operation."""
        if os.getenv("SAFE_MODE", "1") == "1":
            logger.error(
                "Blocked delete attempt due to SAFE_MODE: %s\n%s",
                operation,
                "".join(traceback.format_stack())
            )
            raise ValueError("SAFE_MODE=1 - deletes are disabled")

    
    # ==================== IDEAS OPERATIONS (Task #4583) ====================
    
    def add_idea(self, text: str) -> Dict[str, Any]:
        """Create a new idea.
        
        Args:
            text: Idea text content
            
        Returns:
            Idea dictionary with all fields including generated id and timestamps
            
        Raises:
            ValueError: If validation fails
        """
        if not text or not text.strip():
            raise ValueError("Idea text cannot be empty")
        
        if len(text) > 5000:
            raise ValueError("Idea text too long (max 5000 characters)")
        
        now = datetime.now().isoformat()
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            idea_id = pt_next_id(self.db_path)
            cursor.execute("""
                INSERT INTO ideas (id, text, created_at, updated_at)
                VALUES (?, ?, ?, ?)
            """, (idea_id, text.strip(), now, now))

            conn.commit()
            
            return self.get_idea(idea_id)
    
    def get_idea(self, idea_id: int) -> Optional[Dict[str, Any]]:
        """Get a single idea by ID.
        
        Args:
            idea_id: Idea ID
            
        Returns:
            Idea dictionary or None if not found
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_ideas(self) -> List[Dict[str, Any]]:
        """Get all ideas, ordered by creation date (newest first).
        
        Returns:
            List of idea dictionaries
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ideas ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    def update_idea(self, idea_id: int, text: str) -> Dict[str, Any]:
        """Update an idea's text.
        
        Args:
            idea_id: Idea ID
            text: New text content
            
        Returns:
            Updated idea dictionary
            
        Raises:
            ValueError: If idea not found or invalid text
        """
        if not text or not text.strip():
            raise ValueError("Idea text cannot be empty")
        
        if len(text) > 5000:
            raise ValueError("Idea text too long (max 5000 characters)")
        
        existing_idea = self.get_idea(idea_id)
        if not existing_idea:
            raise ValueError(f"Idea with ID {idea_id} not found")
        
        now = datetime.now().isoformat()
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE ideas 
                SET text = ?, updated_at = ?
                WHERE id = ?
            """, (text.strip(), now, idea_id))
            conn.commit()
            
            return self.get_idea(idea_id)
    
    def delete_idea(self, idea_id: int) -> None:
        """Delete an idea.
        
        Args:
            idea_id: Idea ID
            
        Raises:
            ValueError: If idea not found
        """
        if not self.get_idea(idea_id):
            raise ValueError(f"Idea with ID {idea_id} not found")
        
        # 🛡️ SAFETY: Backup before delete
        self._backup_before_delete(
            table="ideas",
            where_clause="id = ?",
            params=(idea_id,),
            reason=f"Deleting idea: {idea_id}"
        )
        
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ideas WHERE id = ?", (idea_id,))
            conn.commit()

    # -----------------------------------------------------------------
    # Project Info (key-value reference store)
    # -----------------------------------------------------------------

    def get_info(
        self,
        project_id: Optional[str] = None,
        key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get info entries. Filter by project_id and/or key.

        project_id=None returns global entries (where project_id IS NULL).
        project_id="__all__" returns everything.
        """
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if project_id == "__all__":
                if key:
                    cursor.execute(
                        "SELECT id, project_id, key, value, updated_at "
                        "FROM project_info WHERE key = ? "
                        "ORDER BY project_id, key, updated_at DESC, id DESC",
                        (key,),
                    )
                else:
                    cursor.execute(
                        "SELECT id, project_id, key, value, updated_at "
                        "FROM project_info ORDER BY project_id, key, updated_at DESC, id DESC"
                    )
            elif project_id is not None:
                if key:
                    cursor.execute(
                        "SELECT id, project_id, key, value, updated_at "
                        "FROM project_info WHERE project_id = ? AND key = ? "
                        "ORDER BY updated_at DESC, id DESC",
                        (project_id, key),
                    )
                else:
                    cursor.execute(
                        "SELECT id, project_id, key, value, updated_at "
                        "FROM project_info WHERE project_id = ? "
                        "ORDER BY key, updated_at DESC, id DESC",
                        (project_id,),
                    )
            else:
                if key:
                    cursor.execute(
                        "SELECT id, project_id, key, value, updated_at "
                        "FROM project_info WHERE project_id IS NULL AND key = ? "
                        "ORDER BY updated_at DESC, id DESC",
                        (key,),
                    )
                else:
                    cursor.execute(
                        "SELECT id, project_id, key, value, updated_at "
                        "FROM project_info WHERE project_id IS NULL "
                        "ORDER BY key, updated_at DESC, id DESC"
                    )
            rows = cursor.fetchall()
            entries: list[dict[str, Any]] = []
            seen: set[tuple[Optional[str], str]] = set()
            for row in rows:
                entry = dict(row)
                logical_key = (entry["project_id"], entry["key"])
                if logical_key in seen:
                    continue
                seen.add(logical_key)
                entry.pop("id", None)
                entries.append(entry)
            return entries

    def set_info(self, key: str, value: str, project_id: Optional[str] = None) -> None:
        """Set (upsert) an info entry."""
        now = datetime.now(timezone.utc).isoformat()
        info_id = pt_next_id(self.db_path)
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # DELETE + INSERT must be atomic: if INSERT fails, the savepoint
            # rolls back the DELETE so the key is never silently lost.
            cursor.execute("SAVEPOINT set_info")
            try:
                if project_id is None:
                    cursor.execute(
                        "DELETE FROM project_info WHERE project_id IS NULL AND key = ?",
                        (key,),
                    )
                else:
                    cursor.execute(
                        "DELETE FROM project_info WHERE project_id = ? AND key = ?",
                        (project_id, key),
                    )
                cursor.execute(
                    "INSERT INTO project_info (id, project_id, key, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (info_id, project_id, key, value, now),
                )
                cursor.execute("RELEASE set_info")
            except Exception:
                cursor.execute("ROLLBACK TO set_info")
                raise
            conn.commit()

    def delete_info(self, key: str, project_id: Optional[str] = None) -> bool:
        """Delete an info entry. Returns True if a row was deleted."""
        existing = self.get_info(project_id=project_id, key=key)
        if not existing:
            return False
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if project_id is not None:
                cursor.execute(
                    "DELETE FROM project_info WHERE project_id = ? AND key = ?",
                    (project_id, key),
                )
            else:
                cursor.execute(
                    "DELETE FROM project_info WHERE project_id IS NULL AND key = ?",
                    (key,),
                )
            conn.commit()
            return True
