"""In-process project operations shared by the CLI and dashboard."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from .backend_manager import DatabaseManager as BackendDatabaseManager
from .schema import mark_if_holding_data

class ProjectTrackerOps(BackendDatabaseManager):
    """Operations that span tables or need a recovery snapshot, on top of the SQL core."""

    _DESTRUCTIVE_BACKEND_OPS = frozenset({
        "delete_project", "delete_done_tasks", "trim_done_tasks", "raw_import_tasks",
    })

    def __init__(self, db_path=None):
        from scripts.backup_config import external_backup_dir
        super().__init__(db_path)
        self.entry = SimpleNamespace(
            backup_dir=self.db_path.parent / "backups",
            external_backup_dir=external_backup_dir(),
        )

    # The four destructive SQL operations take a verified recovery snapshot
    # first. prepare_operation() calls the core methods directly, so a
    # prepared call still takes exactly one snapshot.
    def delete_project(self, *args, **params):
        self.prepare_destructive(reason="Direct database operation: delete_project")
        return super().delete_project(*args, **params)

    def delete_done_tasks(self, *args, **params):
        self.prepare_destructive(reason="Direct database operation: delete_done_tasks")
        return super().delete_done_tasks(*args, **params)

    def trim_done_tasks(self, *args, **params):
        self.prepare_destructive(reason="Direct database operation: trim_done_tasks")
        return super().trim_done_tasks(*args, **params)

    def raw_import_tasks(self, *args, **params):
        self.prepare_destructive(reason="Direct database operation: raw_import_tasks")
        return super().raw_import_tasks(*args, **params)

    def prepare_destructive(self, *, reason):
        """Verify a fresh recovery snapshot before a confirmed operation."""
        if not isinstance(reason, str) or len(reason.strip()) < 8:
            raise ValueError("A descriptive reason is required")
        path = self.create_recovery_snapshot(label="before_destructive")
        if not self.verify_backup(str(path)):
            raise RuntimeError(f"Recovery snapshot failed verification: {path}")
        return path

    def authorize(self, op, /, *, reason, **params):
        return self.prepare_operation(op, reason=reason, **params)()

    def prepare_operation(self, op, /, *, reason, **params):
        """Verify recovery now; return a single-use call bound to this operation.

        Retirement must establish recovery before moving files, then perform
        its database mutation without another fallible snapshot in between.
        """
        if op not in {"delete_project", "delete_done_tasks", "trim_done_tasks", "raw_import_tasks", "backup_restore", "migrations_apply"}:
            raise ValueError(f"Not a destructive operation: {op}")
        self.prepare_destructive(reason=reason)
        if op in self._DESTRUCTIVE_BACKEND_OPS:
            operation = getattr(super(), op)
        else:
            operation = getattr(self, f"_{op}")
        used = False

        def execute():
            nonlocal used
            if used:
                raise RuntimeError("Prepared database operation was already consumed")
            # A failed backend call may already have committed; never retry it
            # using this preflight. Backend safety checks still run normally.
            used = True
            return operation(**params)

        return execute

    def sync_project_bundle(
        self,
        project: dict,
        agents: list | None = None,
        cron_jobs: list | None = None,
        services: list | None = None,
        health: dict | None = None,
    ) -> dict:
        """Upsert a scanned project and everything hanging off it, atomically.

        Replaces the `BEGIN` / five `_*_with_cursor` calls / `commit` block that
        `pt scan` and `pt project sync` each carried a copy of.

        Returns `{"ok": ...}` rather than raising on a per-project failure,
        because that is the behaviour `pt scan` already had: one bad project
        prints a red line and the scan continues over the rest. The error text
        is returned, never swallowed — a caller that ignores it is the bug, and
        both callers check it.
        """
        db = self
        with db._get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN")
                db._add_project_with_cursor(
                    cursor=cursor,
                    project_id=project["id"],
                    name=project["name"],
                    path=project["path"],
                    status=project["status"],
                    description=project.get("description"),
                    phase=project.get("phase"),
                    last_modified=project["last_modified"],
                    completion_pct=project.get("completion_pct", 0),
                    is_infrastructure=project.get("is_infrastructure", False),
                    has_index=project.get("has_index", False),
                    index_is_valid=project.get("index_is_valid", False),
                    index_updated_at=project.get("index_updated_at"),
                    project_type=project.get("project_type", "standard"),
                )
                db._sync_ai_agents_with_cursor(
                    cursor=cursor, project_id=project["id"], agents=agents or []
                )
                db._sync_cron_jobs_with_cursor(
                    cursor=cursor, project_id=project["id"], cron_jobs=cron_jobs or []
                )
                db._sync_services_with_cursor(
                    cursor=cursor, project_id=project["id"], services=services or []
                )
                if health:
                    db._update_health_with_cursor(
                        cursor=cursor,
                        project_id=project["id"],
                        score=health["score"],
                        grade=health["grade"],
                    )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                return {"ok": False, "project_id": project["id"], "error": str(exc)}
        return {"ok": True, "project_id": project["id"], "error": None}

    # -- schema migrations --------------------------------------------------

    def _migrations_dir(self) -> Path:
        """Migrations shipped alongside the local database implementation."""
        return Path(__file__).resolve().parent / "migrations"

    def migrations_pending(self) -> list:
        from .migration_runner import discover_migrations, unapplied_migrations

        directory = self._migrations_dir()
        if not directory.is_dir():
            return []
        migrations = discover_migrations(directory, verbose=False)
        conn = sqlite3.connect(self.db_path)
        try:
            pending = unapplied_migrations(conn, migrations)
        finally:
            conn.close()
        return [{"version": m.version, "name": m.name} for m in pending]

    def migrations_apply(self) -> dict:
        self.prepare_destructive(reason="Apply database schema migrations")
        return self._migrations_apply()

    def _migrations_apply(self) -> dict:
        from .migration_runner import MigrationError, apply_all

        directory = self._migrations_dir()
        if not directory.is_dir():
            raise FileNotFoundError(f"no migrations directory at {directory}")

        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            try:
                applied = apply_all(conn, directory)
            except MigrationError as err:
                return {"ok": False, "error": str(err), "applied": []}
        finally:
            conn.close()
        return {
            "ok": True,
            "error": None,
            "applied": [{"version": m.version, "name": m.name} for m in applied],
        }

    # -- handoff records ---------------------------------------------------

    _HANDOFF_COLUMNS = (
        "card_id", "project", "branch", "file_list", "intent", "current_status",
        "next_command", "discard_or_preserve_guidance", "record_type",
        "pr_exempt_reason", "pr_exempt_disposition", "pr_exempt_approver",
        "created_at", "created_by",
    )

    def _tracker_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _handoff_row_to_dict(row: sqlite3.Row) -> dict:
        import json

        return {
            "id": row["id"],
            "card_id": row["card_id"],
            "project": row["project"],
            "branch": row["branch"],
            "file_list": json.loads(row["file_list"] or "[]"),
            "intent": row["intent"],
            "current_status": row["current_status"],
            "next_command": row["next_command"],
            "discard_or_preserve_guidance": row["discard_or_preserve_guidance"],
            "record_type": row["record_type"],
            "pr_exempt_reason": row["pr_exempt_reason"],
            "pr_exempt_disposition": row["pr_exempt_disposition"],
            "pr_exempt_approver": row["pr_exempt_approver"],
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "resolved_at": row["resolved_at"],
            "resolved_note": row["resolved_note"],
        }

    def handoff_create(
        self,
        card_id: int,
        branch: str,
        file_list: str,
        intent: str,
        current_status: str,
        next_command: str,
        discard_or_preserve_guidance: str,
        record_type: str,
        created_at: str,
        created_by: str,
        pr_exempt_reason: str | None = None,
        pr_exempt_disposition: str | None = None,
        pr_exempt_approver: str | None = None,
    ) -> dict:
        """Insert a handoff and return it, with the project resolved from the card.

        Returns `{"ok": False, "error_kind": "validation"}` for a missing card
        rather than raising, so the CLI can keep emitting the same
        `pt.handoff.v1` error envelope and the same exit code it always did.
        """
        conn = self._tracker_conn()
        try:
            if conn.execute("SELECT id FROM tasks WHERE id = ?", (card_id,)).fetchone() is None:
                return {
                    "ok": False,
                    "error_kind": "validation",
                    "error": f"card {card_id} does not exist",
                }
            conn.execute(
                f"INSERT INTO handoffs ({', '.join(self._HANDOFF_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(self._HANDOFF_COLUMNS))})",
                (
                    card_id, None, branch, file_list, intent, current_status,
                    next_command, discard_or_preserve_guidance, record_type,
                    pr_exempt_reason, pr_exempt_disposition, pr_exempt_approver,
                    created_at, created_by,
                ),
            )
            handoff_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            task_row = conn.execute(
                "SELECT project_id FROM tasks WHERE id = ?", (card_id,)
            ).fetchone()
            conn.execute(
                "UPDATE handoffs SET project = ? WHERE id = ?",
                (task_row["project_id"] if task_row else None, handoff_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
            ).fetchone()
            return {"ok": True, "error": None, "record": self._handoff_row_to_dict(row)}
        finally:
            conn.close()

    def handoff_list(
        self,
        card_id: int | None = None,
        project: str | None = None,
        unresolved_only: bool = False,
    ) -> list:
        clauses: list[str] = []
        params: list[object] = []
        if card_id is not None:
            clauses.append("card_id = ?")
            params.append(card_id)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if unresolved_only:
            clauses.append("resolved_at IS NULL")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        conn = self._tracker_conn()
        try:
            rows = conn.execute(
                f"SELECT * FROM handoffs {where_sql} ORDER BY created_at DESC", params
            ).fetchall()
            return [self._handoff_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def handoff_show(self, handoff_id: int) -> dict | None:
        conn = self._tracker_conn()
        try:
            row = conn.execute(
                "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
            ).fetchone()
            return self._handoff_row_to_dict(row) if row else None
        finally:
            conn.close()

    def handoff_resolve(
        self, handoff_id: int, resolved_at: str, note: str | None = None
    ) -> dict:
        """Resolve a handoff, refusing to overwrite one already resolved."""
        conn = self._tracker_conn()
        try:
            existing = conn.execute(
                "SELECT id, resolved_at FROM handoffs WHERE id = ?", (handoff_id,)
            ).fetchone()
            if existing is None:
                return {
                    "ok": False,
                    "error_kind": "validation",
                    "error": f"handoff {handoff_id} does not exist",
                }
            if existing["resolved_at"] is not None:
                return {
                    "ok": False,
                    "error_kind": "validation",
                    "error": (
                        f"handoff {handoff_id} is already resolved at "
                        f"{existing['resolved_at']}; refusing to overwrite"
                    ),
                }
            conn.execute(
                "UPDATE handoffs SET resolved_at = ?, resolved_note = ? WHERE id = ?",
                (resolved_at, note, handoff_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
            ).fetchone()
            return {"ok": True, "error": None, "record": self._handoff_row_to_dict(row)}
        finally:
            conn.close()

    # -- migration sessions -------------------------------------------------

    def migration_session_start(
        self, name: str, started_at: str, baseline_head: str, created_by: str
    ) -> dict:
        """Record a bulk-change session. Best-effort, but never silent.

        The state file on disk is the source of truth for revert, so a database
        failure here must not abort `pt migration start`. It previously did
        `except sqlite3.Error: pass`, which meant the row could go missing with
        nothing said. The error is returned instead, and the CLI warns.
        """
        conn = self._tracker_conn()
        try:
            conn.execute(
                "INSERT INTO migrations "
                "(name, project_id, started_at, status, baseline_head, created_by) "
                "VALUES (?, ?, ?, 'recording', ?, ?)",
                (name, None, started_at, baseline_head, created_by),
            )
            db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            return {"ok": True, "error": None, "id": db_id}
        except sqlite3.Error as exc:
            return {"ok": False, "error": str(exc), "id": None}
        finally:
            conn.close()

    def migration_session_finish(
        self, name: str, finished_at: str, status: str,
        manifest_path: str | None = None,
    ) -> dict:
        """Close a recording session. Best-effort, but never silent — see above."""
        if status not in ("finished", "committed", "reverted"):
            raise ValueError(f"unknown migration session status {status!r}")
        conn = self._tracker_conn()
        try:
            cursor = conn.execute(
                "UPDATE migrations SET finished_at = ?, status = ?, manifest_path = ? "
                "WHERE name = ? AND status = 'recording'",
                (finished_at, status, manifest_path, name),
            )
            conn.commit()
            return {"ok": True, "error": None, "rows": cursor.rowcount}
        except sqlite3.Error as exc:
            return {"ok": False, "error": str(exc), "rows": 0}
        finally:
            conn.close()

    def migration_session_list(self) -> list:
        conn = self._tracker_conn()
        try:
            rows = conn.execute(
                "SELECT id, name, project_id, started_at, finished_at, status, "
                "baseline_head, manifest_path, created_by FROM migrations "
                "ORDER BY started_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- dashboard ----------------------------------------------------------

    def upsert_scanned_project(self, project: dict) -> dict:
        """Upsert a rediscovered project's own row, and nothing else.

        Deliberately narrower than `sync_project_bundle`. The dashboard's
        refresh only rediscovers projects — it does not rescan agents, cron
        jobs or services. Routing it through the bundle would pass empty lists
        to the `_sync_*_with_cursor` helpers, which replace rather than merge,
        and silently wipe data the dashboard never intended to touch.
        """
        db = self
        with db._get_conn() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN")
                db._add_project_with_cursor(
                    cursor=cursor,
                    project_id=project["id"],
                    name=project["name"],
                    path=project["path"],
                    status=project["status"],
                    description=project.get("description"),
                    phase=project.get("phase"),
                    last_modified=project["last_modified"],
                    completion_pct=project.get("completion_pct", 0),
                    is_infrastructure=project.get("is_infrastructure", False),
                    has_index=project.get("has_index", False),
                    index_is_valid=project.get("index_is_valid", False),
                    index_updated_at=project.get("index_updated_at"),
                    project_type=project.get("project_type", "standard"),
                )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                return {"ok": False, "project_id": project["id"], "error": str(exc)}
        return {"ok": True, "project_id": project["id"], "error": None}

    def health_snapshot(self) -> dict:
        """Task count and the database path, for /api/health.

        The path is returned because the health payload has always shown it and
        operators use it to confirm which database is live. It is a path the
        caller cannot open, which is rather the point.
        """
        started = time.perf_counter()
        conn = self._tracker_conn()
        try:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        return {
            "task_count": int(count),
            "path": str(self.db_path),
            "query_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def loop_last_executions(self, loop_names: list) -> dict:
        """Most recent execution row per loop name, or None where never run."""
        conn = self._tracker_conn()
        try:
            out: dict = {}
            for name in loop_names:
                row = conn.execute(
                    "SELECT id, started_at, completed_at, status, cards_created, "
                    "error_message FROM loop_executions WHERE loop_name = ? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (name,),
                ).fetchone()
                out[name] = dict(row) if row else None
            return out
        finally:
            conn.close()

    def loop_runs_since(self, since_iso: str) -> list:
        """Recorded runs started at or after ``since_iso`` (an ISO time with offset), oldest first."""
        conn = self._tracker_conn()
        try:
            rows = conn.execute(
                "SELECT loop_name, started_at, status FROM loop_executions WHERE started_at >= ? ORDER BY started_at",
                (since_iso,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def site_stats_recorded(self, site: str, ran_at: str, exit_code: int, sources: str | None) -> None:
        """Store one site's collector run (#8868); sources is JSON or None."""
        conn = self._tracker_conn()
        try:
            conn.execute(
                "INSERT INTO site_stats (site, ran_at, exit_code, sources) VALUES (?, ?, ?, ?)",
                (site, ran_at, exit_code, sources),
            )
            conn.commit()
        finally:
            conn.close()

    def site_stats_since(self, since_iso: str) -> list:
        """Collector runs at or after ``since_iso``, oldest first."""
        conn = self._tracker_conn()
        try:
            rows = conn.execute(
                "SELECT site, ran_at, exit_code, sources FROM site_stats WHERE ran_at >= ? ORDER BY ran_at",
                (since_iso,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def loop_run_started(self, loop_name: str, started_at: str) -> int:
        """Record a run as it starts; returns the row id for loop_run_finished."""
        conn = self._tracker_conn()
        try:
            cursor = conn.execute(
                "INSERT INTO loop_executions (loop_name, started_at, status) VALUES (?, ?, 'running')",
                (loop_name, started_at),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def loop_run_finished(self, run_id: int, completed_at: str, exit_code: int) -> None:
        """Close a run: success on exit 0, else failed with the exit code as its error."""
        conn = self._tracker_conn()
        try:
            conn.execute(
                "UPDATE loop_executions SET completed_at = ?, status = ?, error_message = ? WHERE id = ?",
                (completed_at, "success" if exit_code == 0 else "failed",
                 None if exit_code == 0 else f"exit {exit_code}", run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def task_history_daily(self, start_iso: str, project_id: str | None = None) -> list:
        """Daily review-transition counts from task_history, oldest first.

        Gap-filling across the date range stays in the dashboard: this returns
        only the days that have rows, exactly as the original query did.
        """
        sql = (
            "SELECT DATE(timestamp) as date, "
            "SUM(CASE WHEN old_status = 'Review' AND new_status = 'In Progress' "
            "THEN 1 ELSE 0 END) as review_bounces, "
            "SUM(CASE WHEN old_status = 'Review' AND new_status = 'Done' "
            "THEN 1 ELSE 0 END) as review_promotions, "
            "SUM(CASE WHEN old_status = 'In Progress' AND new_status = 'Review' "
            "THEN 1 ELSE 0 END) as review_entries "
            "FROM task_history WHERE timestamp >= ?"
        )
        params: list = [start_iso]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " GROUP BY DATE(timestamp) ORDER BY date ASC"

        conn = self._tracker_conn()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    # -- backup and restore -------------------------------------------------

    # Retention (owner decision, #8093 D3, 2026-10-09): in each backup directory keep the
    # newest 10 scheduled snapshots and the newest 5 of every other kind.
    _KEEP_SNAPSHOTS = 10
    _KEEP_OTHER = 5
    # A backup's kind is its name before the first timestamp.
    _BACKUP_KIND = re.compile(r"^(?P<kind>.+?)_\d{8}[T_]\d{6}")

    def _backup_dirs(self) -> list[Path]:
        """Both DECISIONS.md backup locations, primary first."""
        return [self.entry.backup_dir, self.entry.external_backup_dir]

    def backup_create(self) -> dict:
        """Timestamped, verified snapshot to both locations.

        Pruning old backups is a separate step (backup_prune), so a failure
        there can never undo or hide a snapshot that succeeded.

        `scripts/backup-db.sh` used to shell out to the `sqlite3` binary with an
        environment variable interpolated into a single-quoted dot-command, so a
        backup path containing a quote broke out of it. It now calls this; there
        is no shell here.
        """
        stamp = time.strftime("%Y%m%d_%H%M%S")
        primary = self.create_recovery_snapshot(label=f"tracker_{stamp}")
        if not self.verify_backup(str(primary)):
            raise RuntimeError(f"backup at {primary} failed verification")

        return {
            "path": str(primary),
            "size_bytes": primary.stat().st_size,
            "verified": True,
        }


    def backup_offsite_copy(self, name: str | None = None) -> dict:
        """Copy a consistent, verified snapshot using the user's rclone config."""
        import shutil
        import subprocess
        from scripts.backup_config import rclone_config_path, rclone_destination
        dest = rclone_destination()
        if not dest:
            raise RuntimeError("Set PT_BACKUP_RCLONE_DEST to the existing offsite backup destination")
        config_path = rclone_config_path()
        if not config_path.is_file():
            raise FileNotFoundError(f"rclone config not found at {config_path}")

        if name:
            source = self._resolve_backup(name)
        else:
            listed = self.backup_list()
            if not listed:
                raise FileNotFoundError("no local backups available to copy offsite")
            source = self._resolve_backup(listed[0]["name"])

        if not self.verify_backup(str(source)):
            raise RuntimeError(f"backup {source.name} failed verification; refusing offsite copy")

        rclone = shutil.which("rclone")
        if not rclone:
            raise FileNotFoundError("rclone binary not found on PATH")

        remote_path = f"{dest.rstrip('/')}/{source.name}"
        cmd = [
            str(rclone),
            "copyto",
            str(source),
            remote_path,
            "--config",
            str(config_path),
            "--checksum",
            "--retries",
            "3",
        ]
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"rclone offsite copy failed: {err[:500]}")

        return {
            "name": source.name,
            "dest": remote_path,
            "size_bytes": source.stat().st_size,
            "verified": True,
        }

    def _backups_to_prune(self, directory: Path) -> list[list[Path]]:
        """Every backup in ``directory`` the retention rule drops, as move units.

        Read-only. A kind (``tracker``, ``before_destructive``,
        ``tasks_delete_backup``, ...) keeps its newest backups by modification
        time: _KEEP_SNAPSHOTS for ``tracker``, _KEEP_OTHER for every other
        kind. A unit is a database followed by its ``-wal``/``-shm`` sidecars,
        or a lone sidecar whose database is gone. Files whose names carry no
        kind and timestamp, or that are not .db/.json backups, are never
        touched. A missing directory has none; any other OS error raises.
        """
        import stat as stat_mode

        try:
            entries = list(directory.iterdir())
        # governance: allow-silent SF002: a backup directory that does not exist holds no backups to prune; any other OSError raises and backup_prune reports it
        except FileNotFoundError:
            return []
        kinds: dict[str, dict[str, list[Path]]] = {}
        mtimes: dict[str, float] = {}
        for path in entries:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat_mode.S_ISREG(info.st_mode):
                continue
            base = path.name.removesuffix("-wal").removesuffix("-shm")
            match = self._BACKUP_KIND.match(base)
            if not match or not base.endswith((".db", ".json")):
                continue
            kinds.setdefault(match["kind"], {}).setdefault(base, []).append(path)
            if path.name == base:
                mtimes[base] = info.st_mtime
        units: list[list[Path]] = []
        for kind, backups in kinds.items():
            keep = self._KEEP_SNAPSHOTS if kind == "tracker" else self._KEEP_OTHER
            # Newest first; the name breaks mtime ties, so a dry run matches the real one.
            present = sorted((b for b in backups if b in mtimes), key=lambda b: (mtimes[b], b), reverse=True)
            kept = set(present[:keep])
            for base, paths in sorted(backups.items()):
                if base not in kept:
                    # The database first, so its sidecars move only after it.
                    units.append(sorted(paths, key=lambda p: (p.name != base, p.name)))
        return units

    def backup_prune(self, dry_run: bool = False) -> dict:
        """Move every unit _backups_to_prune names to the Trash.

        These are real database contents, so they go to the Trash rather than
        being deleted: recoverable if the retention rule turns out wrong. If a
        database cannot be moved, its sidecars stay with it. Any OS error,
        listing the directories or moving a file, is collected in ``errors``
        rather than raised; a file already gone is skipped. With ``dry_run``
        nothing moves and ``pruned`` lists what would.
        """
        from send2trash import send2trash

        pruned: list[str] = []
        errors: list[str] = []
        size = 0
        units: list[list[Path]] = []
        for directory in self._backup_dirs():
            # One unreadable directory is reported; the other is still pruned.
            try:
                units.extend(self._backups_to_prune(directory))
            except OSError as err:
                errors.append(f"listing {directory}: {err}")
        for unit in units:
            has_database = not unit[0].name.endswith(("-wal", "-shm"))
            for index, path in enumerate(unit):
                try:
                    file_size = path.stat().st_size
                    if not dry_run:
                        send2trash(str(path))
                except FileNotFoundError:
                    continue
                except OSError as err:
                    errors.append(f"{path.name}: {err}")
                    if index == 0 and has_database:
                        break  # keep the sidecars with the database left in place
                    continue
                pruned.append(path.name)
                size += file_size
        return {"pruned": pruned, "size_bytes": size, "errors": errors}

    def backup_list(self) -> list:
        """Every restorable snapshot, newest first, named not pathed.

        The name is what `backup_restore` accepts. Paths are returned for display. Restore accepts a bare snapshot name.
        """
        seen: dict[str, dict] = {}
        for directory in self._backup_dirs():
            if not directory.is_dir():
                continue
            for candidate in directory.glob("*.db"):
                info = candidate.stat()
                seen.setdefault(
                    candidate.name,
                    {
                        "name": candidate.name,
                        "size_bytes": info.st_size,
                        "modified": time.strftime(
                            "%Y-%m-%dT%H:%M:%S", time.localtime(info.st_mtime)
                        ),
                        "location": str(directory),
                    },
                )
        return sorted(seen.values(), key=lambda row: row["modified"], reverse=True)

    def _resolve_backup(self, name: str) -> Path:
        """Turn a caller-supplied name into a path inside a configured backup dir.

        The name is a bare filename and is checked as one. Anything with a
        separator, a parent reference, or a resolved location outside the
        backup directories is refused — that is the whole reason restore takes
        a name instead of a path.
        """
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"{name!r} is not a bare backup name")
        for directory in self._backup_dirs():
            candidate = directory / name
            if not candidate.is_file() or candidate.is_symlink():
                continue
            resolved = candidate.resolve()
            if resolved.parent != directory.resolve():
                continue
            return resolved
        raise FileNotFoundError(
            f"no backup named {name!r} in the configured backup directories; "
            "list them with backup_list"
        )

    def backup_restore(self, name: str) -> dict:
        self.prepare_destructive(reason=f"Restore database snapshot {name}")
        return self._backup_restore(name)

    def _backup_restore(self, name: str) -> dict:
        """Restore through SQLite so concurrent handles never retain a replaced inode.

        Both public entry paths verify a recovery snapshot before this operation.
        SQLite's backup transaction coordinates with existing readers/writers;
        do not replace the file or detach its WAL/SHM while other callers exist.
        """
        source = self._resolve_backup(name)
        if source == self.db_path.resolve():
            raise ValueError("refusing to restore from the live database itself")
        if not self.verify_backup(str(source)):
            raise ValueError(f"backup {name!r} failed validation; refusing to restore")
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(self.db_path)
            try:
                deadline = time.monotonic() + 60
                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise TimeoutError("Restore timed out waiting for database access")
                src.backup(dst, pages=256, progress=progress)
                mark_if_holding_data(dst, self.db_path)
            finally:
                dst.close()
        finally:
            src.close()
        return {"restored_from": name, "restored_path": str(self.db_path), "trashed_sidecars": []}

    def create_recovery_snapshot(self, label: str) -> Path:
        """Full timestamped backup to both locations, via SQLite's backup API.

        Uses the online backup API rather than a file copy so a concurrent
        writer cannot produce a torn snapshot — the failure mode that makes a
        `cp` of a WAL database quietly useless.

        DECISIONS.md requires two locations, because the 2026-01-27 incident
        proved one can be lost with the project. These are user-owned local recovery copies.
        """
        stamp = time.strftime("%Y%m%dT%H%M%S") + f"_{time.time_ns()}"
        primary = self.entry.backup_dir / f"{label}_{stamp}.db"
        primary.parent.mkdir(parents=True, exist_ok=True)

        source = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(primary)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()

        external = self.entry.external_backup_dir / f"{label}_{stamp}.db"
        external.parent.mkdir(parents=True, exist_ok=True)
        external.write_bytes(primary.read_bytes())
        return primary

    def verify_backup(self, path: str) -> bool:
        """A backup only counts if it opens, passes integrity_check, and has rows.

        Checking that the file exists is not verification. The gate exists
        because a backup that silently failed is worse than no backup: it
        authorises the destructive operation that then has nothing to fall back
        to.
        """
        candidate = Path(path)
        if not candidate.exists() or candidate.stat().st_size == 0:
            return False
        conn = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"tasks", "projects"} <= tables:
                return False
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] >= 0
        except sqlite3.DatabaseError:  # governance: allow-silent SF002: a backup that cannot be read is not verified; False makes the gate refuse the destructive operation
            return False
        finally:
            conn.close()
