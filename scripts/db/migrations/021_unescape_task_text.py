"""Unescape task text that was HTML-escaped on store (#8246).

Until #8246, add_task and update_task stored ``html.escape(text)``, so pt
and the dashboard showed ``Owner&#x27;s`` and every re-save escaped again.
Text is now stored as typed. This turns each stored escape back into the
text that was typed.

A row is rewritten only when it is exactly what ``html.escape`` produces
for its unescaped form. That is true of every row the old code wrote, and
false of raw text containing a bare ``&``, ``<``, ``>`` or quote, so rows
written raw (before escaping, or by #8246's code) are left alone. Plain
``html.unescape`` on everything would corrupt raw text such as ``foo&notes``,
because HTML5 decodes ``&not`` without a semicolon.

One layer is removed. A row escaped twice by repeated saves keeps one
layer: it cannot be told apart from a user who typed an entity, so it is
left for a person to fix rather than guessed at. Data only; no schema
change, and ``updated_at`` is untouched because the text a person typed
has not changed.
"""

from __future__ import annotations

import html
import sqlite3


def unescaped(text: str) -> str | None:
    """The typed text behind an escaped row, or None if the row is not one."""
    raw = html.unescape(text)
    if raw != text and html.escape(raw, quote=True) == text:
        return raw
    return None


def up(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, text FROM tasks WHERE text LIKE '%&%'").fetchall()
    for task_id, text in rows:
        raw = unescaped(text)
        if raw is not None:
            conn.execute("UPDATE tasks SET text = ? WHERE id = ?", (raw, task_id))
