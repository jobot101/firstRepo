"""
Debug routes — Phase 1.

POST /api/debug
  Receives an error bundle from the machine or UI.
  Returns an SSE stream of Claude's explanation, word by word.
  Flask is the gatekeeper: if debug mode is off, returns 204 immediately.
  Deduplication is applied before any Claude call.

POST /api/debug/toggle
  Enables or disables the debug assistant.

GET  /api/debug/status
  Returns {"enabled": bool}
"""

import json
import time

from flask import Blueprint, Response, request, jsonify, current_app

from services.claude_debug import stream_error_explanation
from utils import deduplicator
from utils.logger import log

debug_bp = Blueprint("debug", __name__)


def _get_settings():
    return current_app.config["SETTINGS"]


# ---------------------------------------------------------------------------
# POST /api/debug  — receive error bundle, stream Claude explanation via SSE
# ---------------------------------------------------------------------------

@debug_bp.route("/api/debug", methods=["POST"])
def handle_debug():
    settings = _get_settings()

    # --- Gatekeeper: return fast if debug is disabled ---
    if not settings.get("debug", {}).get("enabled", True):
        return "", 204

    data = request.get_json(silent=True) or {}
    error_code = data.get("error_code", "UNKNOWN")
    position = data.get("position", {"x": 0, "y": 0, "z": 0})
    window = settings.get("debug", {}).get("dedup_window_seconds", 10)

    # --- Deduplication ---
    if deduplicator.is_duplicate(error_code, position, window_seconds=window):
        log("debug_dedup_dropped", {"error_code": error_code, "position": position})
        return "", 204

    def generate():
        yield from stream_error_explanation(data)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/debug/toggle  — flip the debug toggle
# ---------------------------------------------------------------------------

@debug_bp.route("/api/debug/toggle", methods=["POST"])
def toggle_debug():
    settings = _get_settings()
    body = request.get_json(silent=True) or {}

    # Accept explicit value or flip current state
    if "enabled" in body:
        new_state = bool(body["enabled"])
    else:
        new_state = not settings.get("debug", {}).get("enabled", True)

    settings.setdefault("debug", {})["enabled"] = new_state
    _persist_settings(settings)

    log("debug_toggle", {"enabled": new_state})
    return jsonify({"enabled": new_state})


# ---------------------------------------------------------------------------
# GET /api/debug/status
# ---------------------------------------------------------------------------

@debug_bp.route("/api/debug/status", methods=["GET"])
def debug_status():
    settings = _get_settings()
    enabled = settings.get("debug", {}).get("enabled", True)
    return jsonify({"enabled": enabled})


# ---------------------------------------------------------------------------
# POST /api/debug/gcode-review  — stream Claude review of a G-code file
# ---------------------------------------------------------------------------

@debug_bp.route("/api/debug/gcode-review", methods=["POST"])
def gcode_review_stream():
    """
    Expects JSON: { "filepath": "/path/to/file.nc", "issues": [...] }
    Streams Claude Sonnet's review as SSE.
    """
    settings = _get_settings()
    if not settings.get("debug", {}).get("enabled", True):
        return "", 204

    data = request.get_json(silent=True) or {}
    filepath = data.get("filepath", "")
    issues = data.get("issues", [])

    if not filepath:
        return jsonify({"error": "filepath required"}), 400

    from services.watch_folder import review_file_with_claude

    def generate():
        review = review_file_with_claude(filepath, issues)
        # Split into words for word-by-word streaming effect
        for word in review.split(" "):
            safe = word.replace("\n", " ")
            yield f"data: {safe} \n\n"
            time.sleep(0.02)
        yield "data: [DONE]\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _persist_settings(settings: dict):
    """Write settings back to config/settings.json."""
    import os
    settings_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config", "settings.json"
    )
    try:
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError as exc:
        log("settings_write_error", {"error": str(exc)})
