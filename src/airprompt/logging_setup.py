"""Centralized logging configuration: consistent file logging with rotation.

Every module obtains its logger via ``logging.getLogger(__name__)``; calling
:func:`setup_logging` once at startup attaches the handlers that route those
records to both the console and a rotating log file. Each line is tagged with
the originating module name (``%(name)s``, e.g. ``airprompt.orchestrator``).
"""
from __future__ import annotations

import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOGS_DIR = Path.home() / ".local/share/airprompt/logs"
LOG_FILE = LOGS_DIR / "airprompt.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
RETENTION_DAYS = 30


def _cleanup_old_logs(retention_days: int = RETENTION_DAYS) -> None:
    """Delete log files in LOGS_DIR not modified within ``retention_days``.

    TimedRotatingFileHandler.backupCount only prunes during a rotation event,
    so this sweep is a backstop for the case where the app sits unused long
    enough for old files to outlive the retention window. Failures here must
    never block startup.
    """
    if not LOGS_DIR.is_dir():
        return
    cutoff = time.time() - retention_days * 86_400
    for path in LOGS_DIR.glob("airprompt.log*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def setup_logging(console_level: str = "WARNING") -> None:
    """Configure root logging: console at ``console_level``, file at DEBUG.

    The file handler always records full DEBUG detail so the log file is a
    complete history for debugging crashes even when the console was kept
    quiet. Idempotent: pre-existing root handlers are removed first.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_old_logs()

    formatter = logging.Formatter(LOG_FORMAT)

    root = logging.getLogger()
    # Root level gates every handler, so it must be the most permissive one.
    root.setLevel(logging.DEBUG)
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        backupCount=RETENTION_DAYS,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
