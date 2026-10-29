"""Logging configuration for project tracker."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Rotation caps (#6750). project_tracker.log had grown to 85MB unbounded — it
# was the single largest file in logs/. 10MB x 3 backups caps the whole set at
# ~40MB. Override per-machine with PT_LOG_MAX_BYTES / PT_LOG_BACKUP_COUNT.
#
# Caveat: several processes (the pt CLI, the dashboard) write this file
# concurrently, and RotatingFileHandler rollover is not atomic across
# processes — a rollover racing another writer can drop a few lines. That is an
# acceptable trade for a bounded file; the alternative (WatchedFileHandler plus
# an external rotator) needs a rotator this repo does not own.
LOG_MAX_BYTES = int(os.getenv("PT_LOG_MAX_BYTES", 10 * 1024 * 1024))
LOG_BACKUP_COUNT = int(os.getenv("PT_LOG_BACKUP_COUNT", 3))

# Log directory: PT_LOGS_DIR overrides the default PROJECT_ROOT / "logs".
# Create logs directory when available. Sandboxed agents may have read access to
# project-tracker without write access to its logs directory.
LOGS_DIR = Path(os.getenv("PT_LOGS_DIR")) if os.getenv("PT_LOGS_DIR") else PROJECT_ROOT / "logs"
handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
try:
    LOGS_DIR.mkdir(exist_ok=True)
    handlers.insert(0, RotatingFileHandler(
        LOGS_DIR / 'project_tracker.log',
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    ))
except OSError as err:
    print(f"project-tracker: file logging unavailable: {err}", file=sys.stderr)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers
)

def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a module."""
    return logging.getLogger(name)
