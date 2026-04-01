"""
Watch folder service — Phase 1.5.

Monitors the VCarve output folder for new .nc / .gcode files using the
watchdog library (event-driven, no polling).

When a new file appears:
1. Run gcode_review.analyse() immediately (fast, local)
2. If issues found, emit a SocketIO event to the pendant UI
3. Optionally run a deeper Claude Sonnet review on demand

The service is started once at app startup and runs in a background thread.
"""

import os
import threading
import time
from typing import Callable, Optional

from watchdog.events import FileSystemEventHandler, FileCreatedEvent
from watchdog.observers import Observer

from services import gcode_review
from utils.logger import log_file_review


class _GcodeFileHandler(FileSystemEventHandler):
    def __init__(self, extensions: list[str], on_new_file: Callable[[str, list], None]):
        self._extensions = [e.lower() for e in extensions]
        self._on_new_file = on_new_file

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            path = event.src_path
            ext = os.path.splitext(path)[1].lower()
            if ext in self._extensions:
                self._process(path)

    def _process(self, path: str):
        # Brief delay to let the CAM software finish writing
        time.sleep(0.5)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                gcode_text = f.read()
        except OSError:
            return

        issues = gcode_review.analyse(gcode_text)
        filename = os.path.basename(path)
        log_file_review(filename, issues, "detected")
        self._on_new_file(path, issues)


_observer: Optional[Observer] = None
_lock = threading.Lock()


def start(watch_path: str, extensions: list[str], on_new_file: Callable[[str, list], None]):
    """
    Start the folder watcher in a background thread.
    Safe to call multiple times — only one observer runs at a time.

    on_new_file(path, issues) is called from the watchdog thread whenever
    a new G-code file lands in watch_path.
    """
    global _observer

    with _lock:
        if _observer is not None and _observer.is_alive():
            return  # already running

        os.makedirs(watch_path, exist_ok=True)

        handler = _GcodeFileHandler(extensions, on_new_file)
        _observer = Observer()
        _observer.schedule(handler, watch_path, recursive=False)
        _observer.daemon = True
        _observer.start()


def stop():
    """Stop the folder watcher."""
    global _observer
    with _lock:
        if _observer is not None:
            _observer.stop()
            _observer.join()
            _observer = None


def review_file_with_claude(filepath: str, issues: list) -> str:
    """
    Run a deeper Claude Sonnet review of a G-code file.
    Returns the full review text (blocking).

    Called on demand when the user taps "Review" in the UI.
    """
    import anthropic
    import json

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            gcode_text = f.read()
    except OSError as exc:
        return f"Cannot read file: {exc}"

    # Limit to first 200 lines to keep tokens reasonable
    lines = gcode_text.splitlines()
    truncated = len(lines) > 200
    gcode_preview = "\n".join(lines[:200])
    if truncated:
        gcode_preview += f"\n... ({len(lines) - 200} more lines)"

    issues_str = json.dumps(issues, indent=2) if issues else "None detected by static analysis"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": (
                    "You are reviewing G-code for a Shapeoko 5 Pro CNC (grblHAL, mm, "
                    "X/Y zero at bottom-left, Z zero at spoilboard surface). "
                    "Explain any issues in plain English. Be specific about line numbers. "
                    "Rate overall safety: Safe / Caution / Do Not Run."
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"File: {os.path.basename(filepath)}\n\n"
                    f"Static analysis found these issues:\n{issues_str}\n\n"
                    f"G-code preview:\n```\n{gcode_preview}\n```\n\n"
                    "Please review this file and explain what it does, confirm or expand "
                    "on the issues found, and give an overall safety rating."
                ),
            }
        ],
    )

    for block in response.content:
        if block.type == "text":
            return block.text

    return "(no response)"
