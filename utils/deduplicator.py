"""
Error deduplication logic for the CNC assistant.

Dedup key: error_code + machine position rounded to 1mm.
Window: 10 seconds (configurable).

If the same error fires multiple times within the window, only the first
is passed through — the rest are silently dropped to prevent API spam.
"""

import hashlib
import time
from typing import Optional

# { hash_key: timestamp_of_first_occurrence }
_seen: dict[str, float] = {}


def _round_position(pos: dict) -> tuple:
    """Round x/y/z to nearest mm for dedup purposes."""
    return (
        round(float(pos.get("x", 0))),
        round(float(pos.get("y", 0))),
        round(float(pos.get("z", 0))),
    )


def _make_key(error_code: str, position: dict) -> str:
    rounded = _round_position(position)
    raw = f"{error_code}:{rounded[0]},{rounded[1]},{rounded[2]}"
    return hashlib.sha1(raw.encode()).hexdigest()


def is_duplicate(error_code: str, position: dict, window_seconds: float = 10.0) -> bool:
    """
    Returns True if this error+position was already seen within the window.
    Registers the event if it is new.
    """
    _evict_expired(window_seconds)
    key = _make_key(error_code, position)
    now = time.monotonic()

    if key in _seen:
        return True

    _seen[key] = now
    return False


def _evict_expired(window_seconds: float):
    now = time.monotonic()
    expired = [k for k, t in _seen.items() if now - t > window_seconds]
    for k in expired:
        del _seen[k]


def clear():
    """Clear all dedup state (useful for testing)."""
    _seen.clear()
