"""One Discord message when a calendar block starts (#8795).

The owner sees Discord, not email. The dashboard is the always-running process, so
it checks every 30 seconds and posts '<title> (<project>) now, until HH:MM'
once per block. Sent blocks are kept in data/block_pings.json, so a restart
never sends one twice; a block already 10 minutes old when first seen is
skipped rather than announced late. The webhook is PT_DISCORD_WEBHOOK in
Doppler (project-tracker/dev), read at send time because the dashboard runs
without Doppler. One attempt per block: a failure is logged at ERROR and the
block is marked done, so a broken webhook cannot post or log every 30 seconds.
"""

import json
import os
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from dashboard.common import logger

SENT_FILE = Path(__file__).resolve().parent.parent / "data" / "block_pings.json"
LATE = timedelta(minutes=10)
KEEP = 50


def message(block: dict) -> str:
    project = f" ({block['project_id']})" if block.get("project_id") else ""
    return f"{block['title']}{project} now, until {block['end']:%H:%M}"


def webhook() -> str:
    """The webhook URL from Doppler; raises when Doppler or the secret is missing."""
    return subprocess.run(
        ["doppler", "secrets", "get", "PT_DISCORD_WEBHOOK", "--plain",
         "--project", "project-tracker", "--config", "dev"],
        capture_output=True, text=True, timeout=20, check=True).stdout.strip()


def post(text: str) -> None:
    request = urllib.request.Request(
        webhook(), data=json.dumps({"content": text}).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "pt-dashboard"})
    with urllib.request.urlopen(request, timeout=10):
        pass


def check(now: datetime, current: Optional[dict], send: Callable[[str], None] = post,
          sent_file: Path = SENT_FILE) -> None:
    """Send the current block's ping unless it was sent already or the block is too old."""
    if current is None:
        return
    key = f"{current['id']}@{current['start'].isoformat()}"
    sent = json.loads(sent_file.read_text()) if sent_file.exists() else []
    if key in sent or now - current["start"] >= LATE:
        return
    try:
        send(message(current))
    except Exception as exc:  # any failure: log it visibly and do not retry this block
        logger.error(f"Discord block ping failed for {key}: {type(exc).__name__}: {exc}")
    # Written beside the file and swapped in, so a restart mid-write leaves the old list, not half of one.
    sent_file.parent.mkdir(parents=True, exist_ok=True)
    partial = sent_file.with_suffix(".tmp")
    partial.write_text(json.dumps((sent + [key])[-KEEP:]))
    os.replace(partial, sent_file)


def start() -> None:
    from db.calendar_manager import CalendarManager

    def loop():
        while True:
            try:
                now = datetime.now()
                check(now, CalendarManager().blocks_at(now)["current"])
            except Exception as exc:
                logger.error(f"Discord block ping check failed: {type(exc).__name__}: {exc}")
            time.sleep(30)

    threading.Thread(target=loop, daemon=True, name="block-ping").start()
