"""
Debug routes — Phase 1.

POST /api/debug
  Receives an error bundle. Only escalates to Claude for ALARM codes —
  routine grblHAL errors are logged and dropped silently so normal
  runs are never interrupted.

POST /api/debug/toggle
GET  /api/debug/status
"""

import json
import os

from flask import Blueprint, Response, request, jsonify, current_app

from services.claude_debug import stream_error_explanation
from utils import deduplicator
from utils.logger import log

debug_bp = Blueprint("debug", __name__)

# Only these prefixes are worth waking the user up for.
# Everything else is logged but silently dropped.
_ALARM_PREFIXES = ("ALARM:", "error:", "ERROR:")


def _get_settings():
    return current_app.config["SETTINGS"]


def _is_significant(error_code: str) -> bool:
    """Return True only for codes that actually need the operator's attention."""
    return any(error_code.startswith(p) for p in _ALARM_PREFIXES)


# ── POST /api/debug ────────────────────────────────────────────────────────

@debug_bp.route("/api/debug", methods=["POST"])
def handle_debug():
    settings = _get_settings()

    if not settings.get("debug", {}).get("enabled", True):
        return "", 204

    data = request.get_json(silent=True) or {}
    error_code = data.get("error_code", "UNKNOWN")
    position = data.get("position", {"x": 0, "y": 0, "z": 0})

    # Only ALARM / error codes reach Claude — routine messages drop here.
    if not _is_significant(error_code):
        log("debug_insignificant_dropped", {"error_code": error_code})
        return "", 204

    window = settings.get("debug", {}).get("dedup_window_seconds", 10)
    if deduplicator.is_duplicate(error_code, position, window_seconds=window):
        log("debug_dedup_dropped", {"error_code": error_code})
        return "", 204

    return Response(
        stream_error_explanation(data),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── POST /api/debug/toggle ─────────────────────────────────────────────────

@debug_bp.route("/api/debug/toggle", methods=["POST"])
def toggle_debug():
    settings = _get_settings()
    body = request.get_json(silent=True) or {}
    new_state = bool(body["enabled"]) if "enabled" in body \
        else not settings.get("debug", {}).get("enabled", True)

    settings.setdefault("debug", {})["enabled"] = new_state
    _persist_settings(settings)
    log("debug_toggle", {"enabled": new_state})
    return jsonify({"enabled": new_state})


# ── GET /api/debug/status ──────────────────────────────────────────────────

@debug_bp.route("/api/debug/status", methods=["GET"])
def debug_status():
    enabled = _get_settings().get("debug", {}).get("enabled", True)
    return jsonify({"enabled": enabled})


# ── Helpers ────────────────────────────────────────────────────────────────

def _persist_settings(settings: dict):
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "settings.json")
    try:
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError as exc:
        log("settings_write_error", {"error": str(exc)})
