"""
Structured log writer for the CNC assistant.
Writes JSON-formatted log entries to a rotating log file.
"""

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
LOG_FILE = os.path.join(LOG_DIR, "cnc_assistant.log")
MAX_BYTES = 50 * 1024 * 1024  # 50 MB
BACKUP_COUNT = 3

_logger = None


def _get_logger():
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs(LOG_DIR, exist_ok=True)

    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    _logger = logging.getLogger("cnc_assistant")
    _logger.setLevel(logging.DEBUG)
    _logger.addHandler(handler)

    # Also log to stdout at INFO level
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    _logger.addHandler(console)

    return _logger


def log(event_type: str, data: dict):
    """Write a structured JSON log entry."""
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event": event_type,
        **data,
    }
    _get_logger().info(json.dumps(entry))


def log_error_bundle(bundle: dict):
    """Log an incoming error bundle from the machine or UI."""
    log("error_bundle", {"bundle": bundle})


def log_claude_response(event_type: str, prompt_summary: str, response: str):
    """Log a full Claude response."""
    log(
        "claude_response",
        {
            "type": event_type,
            "prompt_summary": prompt_summary[:200],
            "response_length": len(response),
            "response": response,
        },
    )


def log_job_event(event: str, details: dict):
    """Log a job lifecycle event."""
    log("job_event", {"job_event": event, **details})


def log_file_review(filename: str, issues: list, action: str):
    """Log a watch-folder file review."""
    log("file_review", {"filename": filename, "issues": issues, "action": action})


def log_bit_event(bit_name: str, event: str, details: dict):
    """Log a bit life tracker event."""
    log("bit_event", {"bit": bit_name, "bit_event": event, **details})
