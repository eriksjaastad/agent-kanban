"""Shared paths for user-uploaded task attachments. No database access."""

from __future__ import annotations

from pathlib import Path

ATTACHMENTS_ROOT = Path.home() / ".project-tracker" / "attachments"


def attachments_dir(task_id: int, *, create: bool = True) -> Path:
    """Return (and by default create) the storage directory for a task."""
    base = ATTACHMENTS_ROOT / str(int(task_id))
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def delete_attachment_files(attachments: list[dict]) -> None:
    """Trash attachment files and remove empty bookkeeping directories. Log failures without hiding committed database changes."""
    import logging

    from send2trash import send2trash

    logger = logging.getLogger(__name__)

    for attachment in attachments or []:
        task_id = attachment.get("task_id")
        stored_name = attachment.get("stored_name")
        if task_id is None or not stored_name:
            continue

        attachment_dir = attachments_dir(int(task_id), create=False)
        file_path = attachment_dir / str(stored_name)
        try:
            if file_path.exists():
                send2trash(str(file_path))
            # The now-empty per-task directory is our own bookkeeping, not
            # user content, so removing it outright is fine — but only when
            # it is genuinely empty.
            if attachment_dir.exists() and not any(attachment_dir.iterdir()):
                attachment_dir.rmdir()  # governance: allow-delete DS001: our own bookkeeping dir, proven empty by the guard above; the user content in it went to the Trash
        except OSError as exc:
            logger.warning(
                "Failed to clean attachment file for task %s (%s): %s",
                task_id,
                stored_name,
                exc,
            )
