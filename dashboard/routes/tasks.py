"""The Tasks API behind the Kanban board: cards, checklists and attachments."""

import uuid
from pathlib import Path
from typing import Dict, List, Optional

from db.attachment_paths import attachments_dir
from db.backend_manager import NotesConflictError
from db.manager import DatabaseManager
from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from dashboard.common import _enrich_task_payloads_with_display_ids, _serialized, logger
from scripts.utils.checklist import ChecklistLineError, toggle_checklist_line

router = APIRouter()


# Pydantic models for task requests
class TaskCreateRequest(BaseModel):
    text: str
    project_id: str
    status: Optional[str] = "Backlog"
    priority: Optional[str] = None
    task_type: Optional[str] = None
    category: Optional[str] = None
    parent_id: Optional[int] = None  # Task #4645
    blocked_by: Optional[List[int]] = None  # Task #4579
    # Acceptance criteria, required (#7608): must contain at least one
    # "- [ ] <text>" checklist line — see validate_acceptance_criteria().
    notes: Optional[str] = None


class ChecklistToggleRequest(BaseModel):
    """Body for POST /api/tasks/{id}/checklist (#7821).

    ``base_notes`` is the exact notes text the client rendered the checkbox
    from. It is what identifies the line: an index plus item text cannot tell
    two identical items apart once another writer moves or removes one.
    """
    line_index: int
    expected_text: str
    checked: bool
    base_notes: Optional[str]


CHECKLIST_TOGGLE_MAX_ATTEMPTS = 3


class TaskUpdateRequest(BaseModel):
    text: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None
    commit_sha: Optional[str] = None
    category: Optional[str] = None
    review_comment: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    task_type: Optional[str] = None
    parent_id: Optional[int] = None  # Task #4645
    blocked_by: Optional[List[int]] = None  # Task #4579
    # The notes the edit started from. When sent with `notes`, the write only
    # applies if the stored notes still equal it; otherwise 409 (#7821).
    base_notes: Optional[str] = None


def _extract_field_from_error(error_message: str) -> Optional[str]:
    """Extract field name from error message."""
    # Common patterns: "Task text...", "Project ID...", "Status...", "Priority..."
    if "acceptance criteria" in error_message.lower():
        # #7608: create_card() rejects a missing/malformed checklist in
        # notes before add_task() ever runs.
        return "notes"
    elif "task text" in error_message.lower() or "text" in error_message.lower():
        return "text"
    elif "project" in error_message.lower():
        return "project_id"
    elif "status" in error_message.lower():
        return "status"
    elif "priority" in error_message.lower():
        return "priority"
    return None


def _extract_pattern_from_error(error_message: str) -> Optional[str]:
    """Extract secret pattern from error message if present."""
    if "pattern:" in error_message.lower():
        parts = error_message.split("pattern:")
        if len(parts) > 1:
            return parts[1].strip()
    return None


@router.post("/api/tasks", status_code=status.HTTP_201_CREATED)
@_serialized
def create_task(task_data: TaskCreateRequest):
    """Create a new task.

    Args:
        task_data: Task creation data (text, project_id, status, priority,
            notes). ``notes`` must include at least one "- [ ] <text>"
            acceptance-criteria checklist line (#7608) — enforced by
            ``DatabaseManager.create_card()``, not ``add_task()``. A missing
            or malformed checklist returns 400 with a message naming the
            exact format expected; see ``_extract_field_from_error``.

    Returns:
        Created task object with ID and timestamps

    Raises:
        HTTPException: 400 if validation fails, 404 if project not found
    """
    try:
        db = DatabaseManager()
        
        # Convert blocked_by list to JSON string if provided
        blocked_by_json = None
        if task_data.blocked_by:
            import json
            blocked_by_json = json.dumps(task_data.blocked_by)
        
        task = db.create_card(
            text=task_data.text,
            project_id=task_data.project_id,
            status=task_data.status or "Backlog",
            priority=task_data.priority,
            task_type=task_data.task_type,
            category=task_data.category,
            parent_id=task_data.parent_id,
            blocked_by=blocked_by_json,
            notes=task_data.notes,
        )
        return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error creating task: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create task"
        )


@router.get("/api/tasks")
def list_tasks(
    project_id: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    task_status: Optional[str] = None,
    include_subtasks: bool = True,
    parent_id: Optional[int] = None,
    include_archived: bool = False
):
    """List tasks with optional filtering.

    Args:
        project_id: Filter by project ID (optional)
        status_filter: Filter by status (optional; canonical query param)
        task_status: Legacy filter alias preserved for compatibility
        include_subtasks: Include subtasks in results (default: True)
        parent_id: Filter to subtasks of specific parent (optional)
        include_archived: Include Done cards past the per-project display
            cap (default: False, matching the board)

    Returns:
        Dictionary with enriched tasks list and total count
    """
    try:
        db = DatabaseManager()
        effective_status = status_filter if status_filter is not None else task_status
        tasks = db.get_tasks(
            project_id=project_id,
            status=effective_status,
            include_archived=include_archived,
        )

        subtasks_by_parent: Dict[int, List[Dict[str, object]]] = {}
        task_lookup = {task["id"]: dict(task) for task in tasks}

        # Blockers that are not in this fetch get resolved individually below.
        # `task_lookup` is scoped to one project's tasks and `blocked_by` is
        # not project-scoped at all, so a cross-project blocker — an intended
        # use case — misses on EVERY request, not just for archived or missing
        # ids. Resolving those uncached would be an N+1 against a sidebar that
        # calls this endpoint once per project. Cache per request, so each
        # distinct id costs at most one lookup no matter how many cards
        # reference it, and a card blocked by the same parent as its siblings
        # costs one query for all of them.
        blocker_cache: Dict[int, Optional[Dict[str, object]]] = {}

        def _resolve_blocker(blocked_id):
            if blocked_id in task_lookup:
                return task_lookup[blocked_id]
            if blocked_id not in blocker_cache:
                blocker_cache[blocked_id] = db.get_task(blocked_id)
            return blocker_cache[blocked_id]

        for task in tasks:
            parent = task.get("parent_id")
            if parent is not None:
                subtasks_by_parent.setdefault(parent, []).append(dict(task))

        # Filter by parent_id if specified
        if parent_id is not None:
            tasks = [t for t in tasks if t.get("parent_id") == parent_id]

        # Filter out subtasks if include_subtasks is False
        if not include_subtasks:
            tasks = [t for t in tasks if t.get("parent_id") is None]

        # Enrich each task with subtask info and blocking status
        # NOTE: Subtasks feature (#4645) is incomplete - methods not yet implemented
        enriched_tasks = []
        for task in tasks:
            task_dict = dict(task)

            # Build subtask metadata from the already-fetched task set so we
            # do not open a new SQLite connection for every row in the board.
            subtasks = subtasks_by_parent.get(task["id"], []) if include_subtasks else []
            if subtasks:
                ordered_subtasks = sorted(
                    subtasks,
                    key=lambda item: (
                        item.get("sequence_order") is None,
                        item.get("sequence_order") if item.get("sequence_order") is not None else 0,
                        item.get("created_at", ""),
                    ),
                )
                done_count = len([item for item in ordered_subtasks if item.get("status") == "Done"])
                task_dict["subtasks"] = ordered_subtasks
                task_dict["subtask_progress"] = {
                    "total": len(ordered_subtasks),
                    "done": done_count,
                    "percent": int(done_count / len(ordered_subtasks) * 100),
                }

            # Resolve blocking data from the same in-memory task set to avoid
            # N additional DB reads while rendering the Kanban board.
            if task.get("blocked_by"):
                try:
                    blocked_by_ids, blocked_by_error = db.parse_blocked_by(task)
                    if blocked_by_error:
                        # Fail closed (#6900): an unreadable blocked_by renders
                        # the card blocked, with the reason, not unblocked.
                        raise ValueError(blocked_by_error)
                    task_dict["blocked_by_ids"] = blocked_by_ids

                    # A miss in task_lookup used to be dropped silently, so a
                    # card whose blockers were not in the fetched set rendered
                    # as unblocked — the dashboard's version of the #6747 bug,
                    # where "(all resolved)" meant the opposite.
                    #
                    # A miss has two very different causes and they must not be
                    # conflated: the blocker may be archived (Done, and
                    # excluded by default since #6870 — genuinely not blocking),
                    # or it may not exist at all. Fall back to a PK lookup,
                    # which ignores archive state, and only then call it
                    # unresolvable.
                    blocking_tasks = []
                    unresolved_ids = []
                    for blocked_id in blocked_by_ids:
                        blocking_task = _resolve_blocker(blocked_id)
                        if blocking_task is None:
                            unresolved_ids.append(blocked_id)
                        else:
                            blocking_tasks.append(blocking_task)

                    incomplete_ids = [
                        blocking_task["id"]
                        for blocking_task in blocking_tasks
                        if blocking_task.get("status") != "Done"
                    ]
                    task_dict["blocking_tasks"] = blocking_tasks
                    # An id we cannot resolve is not proof of "not blocked".
                    # Fail towards blocked and name it, so a bad reference is
                    # visible rather than silently clearing the flag.
                    task_dict["is_blocked"] = bool(incomplete_ids or unresolved_ids)
                    task_dict["incomplete_blocking_ids"] = incomplete_ids
                    task_dict["unresolved_blocking_ids"] = unresolved_ids
                    if unresolved_ids:
                        logger.warning(
                            "Task %s references blocking ids that do not exist: %s",
                            task.get("id"), unresolved_ids,
                        )
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "Could not resolve blocked_by for task %s: %s",
                        task.get("id"),
                        exc,
                    )
                    task_dict.update(
                        blocked_by_ids=[],
                        blocking_tasks=[],
                        is_blocked=True,
                        incomplete_blocking_ids=[],
                        unresolved_blocking_ids=[],
                        blocked_by_error=str(exc),
                    )

            enriched_tasks.append(task_dict)

        enriched_tasks = _enrich_task_payloads_with_display_ids(enriched_tasks, db)
        return {
            "tasks": enriched_tasks,
            "total": len(enriched_tasks)
        }
    except Exception as e:
        logger.error(f"Error listing tasks: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve tasks"
        )


@router.get("/api/tasks/{task_id:int}")
def get_task(task_id: int):
    """Get a single task by ID with full enriched data.
    
    Args:
        task_id: Task ID
        
    Returns:
        Enriched task object with subtasks, parent, and blocking info
        
    Raises:
        HTTPException: 404 if task not found
    """
    try:
        db = DatabaseManager()
        task = db.get_task(task_id)
        
        if not task:
            raise ValueError(f"Task with ID {task_id} not found")
        
        task_dict = dict(task)
        
        # Add subtasks if this is a parent
        subtasks = db.get_subtasks(task_id)
        if subtasks:
            task_dict["subtasks"] = subtasks
            task_dict["subtask_progress"] = db.get_subtask_progress(task_id)
        
        # Add parent info if this is a subtask
        if task.get("parent_id"):
            parent = db.get_task(task["parent_id"])
            if parent:
                task_dict["parent"] = parent
        
        # Add blocking info if task has blocked_by
        # An unreadable blocked_by fails closed (#6900): the card is blocked
        # and the payload names why, instead of the fields silently vanishing.
        if task.get("blocked_by"):
            blocked_by_ids, blocked_by_error = db.parse_blocked_by(task)
            task_dict["blocked_by_ids"] = blocked_by_ids
            task_dict["blocking_tasks"] = db.get_blocking_tasks(task_id)
            is_blocked, blocking_ids = db.is_blocked(task_id)
            task_dict["is_blocked"] = is_blocked
            task_dict["incomplete_blocking_ids"] = blocking_ids
            if blocked_by_error:
                task_dict["blocked_by_error"] = blocked_by_error
        
        return _enrich_task_payloads_with_display_ids([task_dict], db)[0]
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except Exception as e:
        logger.error(f"Error retrieving task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve task"
        )


@router.patch("/api/tasks/{task_id:int}")
@_serialized
def update_task(task_id: int, task_data: TaskUpdateRequest):
    """Update a task (text, status, priority).
    
    Args:
        task_id: Task ID
        task_data: Fields to update (text, status, priority)
        
    Returns:
        Updated task object
        
    Raises:
        HTTPException: 400 if validation fails, 404 if task not found, 409 if
            base_notes is sent with notes and no longer matches the stored notes
        
    Note:
        Status changes are automatically recorded in task_history. Notes
        changes are automatically recorded in task_notes_history.
    """
    try:
        db = DatabaseManager()
        provided_fields = getattr(task_data, "model_fields_set", None)
        if provided_fields is None:
            provided_fields = getattr(task_data, "__fields_set__", set())
        
        # Build update dict from non-None fields
        updates = {}
        if "text" in provided_fields:
            updates["text"] = task_data.text
        if "title" in provided_fields:
            updates["title"] = task_data.title
        if "notes" in provided_fields:
            updates["notes"] = task_data.notes
        if "commit_sha" in provided_fields:
            updates["commit_sha"] = task_data.commit_sha
        if "category" in provided_fields:
            updates["category"] = task_data.category
        if "review_comment" in provided_fields:
            updates["review_comment"] = task_data.review_comment
        if "status" in provided_fields:
            updates["status"] = task_data.status
        if "priority" in provided_fields:
            updates["priority"] = task_data.priority
        if "task_type" in provided_fields:
            updates["task_type"] = task_data.task_type
        if "parent_id" in provided_fields:
            updates["parent_id"] = task_data.parent_id
        if "blocked_by" in provided_fields:
            import json
            updates["blocked_by"] = (
                json.dumps(task_data.blocked_by) if task_data.blocked_by is not None else None
            )
        
        # If-Match: a stale base is answered before any validation, since the
        # edit's other values were chosen against notes that no longer hold.
        # update_task's `notes IS ?` still guards a write landing after this read.
        stale_notes = HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The notes changed since this edit started; nothing was saved.",
        )
        if "notes" in updates and "base_notes" in provided_fields:
            current = db.get_task(task_id)
            if current and current.get("notes") != task_data.base_notes:
                raise stale_notes
            updates["expected_notes"] = task_data.base_notes

        # Prompt check for In Progress — warn but don't block
        if updates.get("status") == "In Progress":
            current = db.get_task(task_id)
            if not current:
                raise ValueError(f"Task with ID {task_id} not found")
            prompt_value = updates.get("prompt")
            if prompt_value is None:
                prompt_value = current.get("prompt")
            task_type_value = updates.get("task_type")
            if task_type_value is None:
                task_type_value = current.get("task_type") or "manual"
            if task_type_value == "agent" and not prompt_value:
                logger.warning(f"Agent task #{task_id} started without a prompt")
            is_blocked, blocking_ids = db.is_blocked(task_id)
            if is_blocked and not blocking_ids:
                # Fail closed (#6900): an unreadable blocked_by blocks the start.
                reason = db.blocked_by_error(task_id) or "blocked_by unreadable"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot start task while blocked: {reason}"
                )
            if is_blocked:
                raw_blockers = db.get_task_display_id_map(task_ids=list(blocking_ids))
                blocking_display_map = {int(k): int(v) for k, v in (raw_blockers or {}).items()}
                blocking_str = ", ".join(
                    f"#{blocking_display_map.get(int(bid), bid)}" for bid in blocking_ids
                )
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot start task while blocked by: {blocking_str}"
                )

        if not updates:
            # No updates provided, return current task
            task = db.get_task(task_id)
            if not task:
                raise ValueError(f"Task with ID {task_id} not found")
            return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
        
        # update_task handles history recording for status and notes changes
        try:
            task = db.update_task(task_id, notes_source="api", **updates)
        except NotesConflictError:
            raise stale_notes
        return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
    except ValueError:
        # Re-raise to be caught by error handler
        raise
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update task"
        )


@router.post("/api/tasks/{task_id:int}/checklist")
@_serialized
def toggle_task_checklist(task_id: int, body: ChecklistToggleRequest):
    """Tick or untick one notes checklist line as a server-side atomic toggle (#7821).

    The toggle applies only to the notes the client saw: if the stored notes
    differ from ``base_notes`` in any way, nothing is written and the answer
    is 409, so the client reloads and the user clicks again. Otherwise the
    server sets only the marker of ``line_index`` (which must be the
    checklist line whose item text is ``expected_text``) and writes with a
    compare-and-swap on the notes it read, so a writer that lands between
    the read and the write is never overwritten.

    404 when the task is missing, including one deleted while this request
    runs; 409 when the notes changed or the line is not the expected item.
    A toggle already in the requested state writes nothing and returns the
    task as read.
    """
    def not_found() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task with ID {task_id} not found",
        )

    def changed() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The notes changed since they were loaded; reload and try again",
        )

    try:
        db = DatabaseManager()
        for _ in range(CHECKLIST_TOGGLE_MAX_ATTEMPTS):
            current = db.get_task(task_id)
            if not current:
                raise not_found()
            old_notes = current.get("notes")
            if (old_notes or "") != (body.base_notes or ""):
                raise changed()
            try:
                new_notes = toggle_checklist_line(
                    old_notes, body.line_index, body.expected_text, body.checked
                )
            except ChecklistLineError as e:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
            if new_notes == old_notes:
                return _enrich_task_payloads_with_display_ids([dict(current)], db)[0]
            try:
                task = db.update_task(
                    task_id, notes_source="api", expected_notes=old_notes, notes=new_notes
                )
            except NotesConflictError:
                # Another write (or a delete) landed after our read. Re-read:
                # a changed note is a 409 above, a deleted task a 404.
                continue
            except ValueError:
                # update_task raises ValueError when the task vanished between
                # our read and its own.
                if not db.get_task(task_id):
                    raise not_found()
                raise
            if task is None:
                # Written, then deleted before update_task could re-read it.
                raise not_found()
            return _enrich_task_payloads_with_display_ids([dict(task)], db)[0]
        # Every attempt lost its compare-and-swap. Report what is true now.
        if not db.get_task(task_id):
            raise not_found()
        raise changed()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error toggling checklist on task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to toggle checklist item"
        )


@router.delete("/api/tasks/{task_id:int}", status_code=status.HTTP_200_OK)
@_serialized
def delete_task(task_id: int):
    """Delete a single task."""
    try:
        db = DatabaseManager()
        db.delete_task(task_id)
        # str(): task_id is a pt_id Snowflake int (#6044) that can exceed
        # Number.MAX_SAFE_INTEGER; a bare JSON number would round in the
        # browser (#7824).
        return {"deleted": True, "task_id": str(task_id)}
    except ValueError as e:
        message = str(e)
        if "SAFE_MODE=1" in message:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=message
            )
        if "not found" in message.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message
        )
    except Exception as e:
        logger.error(f"Error deleting task {task_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete task"
        )


ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def _stringify_attachment_ids(record: dict) -> dict:
    """Stringify an attachment record's Snowflake-scale id fields (#7824).

    ``task_attachments.id`` and ``.task_id`` both come from pt_next_id
    (#6044) and can exceed Number.MAX_SAFE_INTEGER; left as bare JSON
    numbers they round in the browser the same way task ids did.
    """
    for key in ("id", "task_id"):
        if isinstance(record.get(key), int):
            record[key] = str(record[key])
    return record


@router.post("/api/tasks/{task_id:int}/attachments", status_code=status.HTTP_201_CREATED)
@_serialized
def upload_attachment(task_id: int, file: UploadFile = File(...)):
    """Upload a file and attach it to a task."""
    import mimetypes

    db = DatabaseManager()
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    # The request parser has already spooled the upload; copy it in chunks so
    # we never hold >ATTACHMENT_MAX_BYTES in RAM.
    dest_dir = attachments_dir(task_id)
    ext = Path(file.filename or "upload").suffix
    stored_name = f"{uuid.uuid4()}{ext}"
    dest_path = dest_dir / stored_name

    size = 0
    try:
        with dest_path.open("wb") as out:
            while chunk := file.file.read(65_536):  # 64 KB chunks
                size += len(chunk)
                if size > ATTACHMENT_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File exceeds 20 MB limit")
                out.write(chunk)
        mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
        record = db.add_attachment(
            task_id=task_id,
            filename=file.filename or stored_name,
            stored_name=stored_name,
            mime_type=mime_type,
            size_bytes=size,
        )
    except BaseException:
        # No row points at a partial, oversized or unrecorded file.
        dest_path.unlink(missing_ok=True)  # governance: allow-delete DS001: this request created the file moments ago and no attachment row references it
        raise
    return _stringify_attachment_ids(record)


@router.get("/api/tasks/{task_id:int}/attachments")
def list_attachments(task_id: int):
    """List all attachments for a task."""
    db = DatabaseManager()
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"attachments": [_stringify_attachment_ids(a) for a in db.get_attachments(task_id)]}


@router.delete("/api/tasks/{task_id:int}/attachments/{attachment_id}", status_code=status.HTTP_200_OK)
@_serialized
def delete_attachment(task_id: int, attachment_id: int):
    """Delete an attachment record and its file from disk."""
    db = DatabaseManager()
    record = db.delete_attachment(attachment_id=attachment_id, task_id=task_id)
    if not record:
        raise HTTPException(status_code=404, detail="Attachment not found")
    file_path = attachments_dir(task_id, create=False) / record["stored_name"]
    if file_path.exists():
        # User-uploaded content goes to the Trash, recoverable. A misclick in
        # the UI should not destroy a file outright (#6905, #6899).
        from send2trash import send2trash
        try:
            send2trash(str(file_path))
        except OSError as exc:
            # The DB row is already gone and committed by this point, so raising
            # here would 500 the request while leaving the file orphaned on disk
            # and invisible to the UI — losing track of it rather than losing it.
            # Report the deletion that did happen and name the file loudly enough
            # that it can be reclaimed by hand.
            logger.error(
                "Attachment row %s deleted but its file could not be trashed; "
                "orphaned on disk at %s: %s",
                attachment_id, file_path, exc,
            )
            return {
                "deleted": True,
                "attachment_id": str(attachment_id),
                "file_orphaned": str(file_path),
            }
    return {"deleted": True, "attachment_id": str(attachment_id)}


@router.get("/api/attachments/{task_id}/{stored_name}")
def serve_attachment(task_id: int, stored_name: str):
    """Serve an attachment file for inline preview."""
    import mimetypes

    from fastapi.responses import FileResponse
    db = DatabaseManager()
    attachment = db.get_attachment(task_id, stored_name)
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")
    attach_dir = attachments_dir(task_id, create=False)
    file_path = (attach_dir / stored_name).resolve()
    # Path traversal guard: resolved path must stay inside the task's attachment dir
    if not file_path.is_relative_to(attach_dir.resolve()):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(stored_name)[0] or "application/octet-stream"
    return FileResponse(path=str(file_path), media_type=media_type)
